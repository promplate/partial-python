"""Sequential execution of original source under explicit interactive semantics."""

import __future__

import ast
import asyncio
import inspect
import sys
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing
from dataclasses import dataclass, field
from time import monotonic
from types import CodeType
from typing import Any, Literal, cast

from ._blocks import BlockEvent, BlockParser
from ._contexts import Scope, compile_binding, reraise
from .blocks import Block
from .parser import _FUTURES, CodeUnit, Parser, _source_line, _tree


def _character_column(text: str, byte_column: int | None) -> int | None:
    if byte_column is None or byte_column <= 0:
        return byte_column
    return len(text.encode()[: byte_column - 1].decode(errors="replace")) + 1


ExecutorState = Literal["open", "finished", "failed", "aborted"]


class UnsupportedSyntaxError(SyntaxError):
    """Syntax whose cross-unit semantics cannot be preserved by this executor."""


@dataclass(frozen=True, slots=True)
class ParserState:
    """Immutable parser snapshot; read ``executor.parser_state`` again for current values.

    ``committed`` counts source characters sealed by parsing, not executed code.
    ``closed`` records parser EOF, not the executor's terminal state.
    """

    pending: str
    committed: int
    closed: bool


@dataclass(frozen=True, slots=True)
class Execution:
    """One successfully completed unit, with zero-based index and monotonic-second timestamps.

    Timing includes compilation and scheduling; an absent event does not prove
    that a failed or cancelled unit had no effects.
    """

    index: int
    unit: CodeUnit
    started_at: float
    completed_at: float


class _ModuleScope:
    def __init__(self) -> None:
        self.annotations: list[ast.AnnAssign] = []
        self.globals: list[ast.Global] = []

    def visit(self, tree: ast.AST) -> None:
        pending = [tree]
        while pending:
            node = pending.pop()
            # Declarations inside another scope cannot affect earlier units.
            # Expressions cannot contain either declaration; skipping them also
            # avoids Python recursion limits on otherwise valid deep expressions.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.expr)):
                continue
            if isinstance(node, ast.AnnAssign):
                # Even non-simple targets can reset deferred module metadata on
                # 3.14+, despite not creating a new __annotate__ function themselves.
                self.annotations.append(node)
            elif isinstance(node, ast.Global):
                self.globals.append(node)
            else:
                pending.extend(reversed(list(ast.iter_child_nodes(node))))


class _ModuleParser(Parser):
    """Buffer a whole compilation unit without speculative parsing or EOF repair."""

    def feed(self, delta: str) -> tuple[CodeUnit, ...]:
        self._ensure_open()
        if not isinstance(delta, str):
            raise TypeError("delta must be str")
        if delta:
            self._chunks.append(delta)
        return ()

    def finish(self) -> tuple[CodeUnit, ...]:
        self._ensure_open()
        self._closed = True
        source = self.pending
        self._pending = ""
        return (self._statement(source),)


@dataclass(slots=True)
class _Frame:
    started: float
    active: bool
    parent_active: bool
    taken: bool = False
    scopes: list[Scope] = field(default_factory=list)


@dataclass(slots=True)
class _Evaluation:
    value: Any = None
    error: BaseException | None = None

    def unwrap(self) -> Any:
        if self.error is not None:
            reraise(self.error)
        return self.value


def _has_frame(error: BaseException, code: CodeType) -> bool:
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code is code:
            return True
        traceback = traceback.tb_next
    return False


class Executor:
    """One append-only execution stream sharing a caller-owned global namespace.

    Failure and cancellation are terminal: earlier effects remain, and accepted
    statements are never retried. This is an in-process executor, not a sandbox.
    Concurrent or recursive writes are rejected rather than silently reordered.
    """

    def __init__(self, namespace: dict[str, Any] | None = None, *, filename: str = "<partial-python>", stream_blocks: Block = Block.MODULE):
        if not isinstance(stream_blocks, Block):
            raise TypeError("stream_blocks must be a Block member or combination")
        self._stream_blocks = stream_blocks
        self._namespace = namespace if namespace is not None else {"__name__": "__main__"}
        self._parser: Parser | BlockParser
        if Block.MODULE not in stream_blocks:
            self._parser = _ModuleParser(filename=filename)
        elif stream_blocks == Block.MODULE:
            self._parser = Parser(filename=filename)
        else:
            self._parser = BlockParser(filename=filename, stream_blocks=stream_blocks)
        self._frames: list[_Frame] = []
        self._owner: asyncio.Task[Any] | None = None
        self._started_block: int | None = None
        self._events: list[Execution] = []
        self._flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        self._prologue = True
        self._first = True
        self._busy = False
        self._state: ExecutorState = "open"
        self._failed_unit: CodeUnit | None = None
        self._error: BaseException | None = None
        self._queued: deque[CodeUnit | BlockEvent] = deque()

    @property
    def stream_blocks(self) -> Block:
        return self._stream_blocks

    @property
    def namespace(self) -> dict[str, Any]:
        return self._namespace

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def failed_unit(self) -> CodeUnit | None:
        return self._failed_unit

    @property
    def parser_state(self) -> ParserState:
        """Snapshot of parsing progress, separate from completed execution events."""
        return ParserState(self._parser.pending, self._parser.committed, self._parser.closed)

    @property
    def queued(self) -> tuple[CodeUnit, ...]:
        """Sealed units not yet started; excludes the unsealed ``parser_state.pending`` tail."""
        return tuple(
            item if isinstance(item, CodeUnit) else item.unit
            for item in self._queued
            if isinstance(item, CodeUnit) or item.depth == 0 and item.kind in {"statement", "exit"} and item.unit.start != self._started_block
        )

    @property
    def events(self) -> tuple[Execution, ...]:
        return tuple(self._events)

    @property
    def state(self) -> ExecutorState:
        return self._state

    def _enter(self) -> None:
        if self._busy:
            raise RuntimeError("executor already has an active operation")
        if self._state != "open":
            raise RuntimeError(f"executor is {self._state}")
        self._check_owner()
        self._busy = True

    def _check_owner(self) -> None:
        if self._frames and self._owner is not asyncio.current_task():
            raise RuntimeError("open streaming blocks must be resumed and closed in their owning task")

    def _syntax_error(self, error_type: type[SyntaxError], message: str, statement: CodeUnit, node: ast.stmt, *, shifted: bool = False) -> SyntaxError:
        line = node.lineno - (statement.start_line - 1 if shifted else 0)
        text = _source_line(statement.source, line)
        column = _character_column(text, node.col_offset + 1)
        return error_type(message, (self._parser.filename, statement.start_line + line - 1, column, text))

    def _compile(self, statement: CodeUnit, tree: ast.Module | None = None) -> CodeType:
        if Block.MODULE not in self._stream_blocks:
            # Whole-module mode compiles once and keeps native global/annotation
            # semantics. Incremental-unit restrictions do not apply here.
            return compile(statement.source, self._parser.filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
        shifted = tree is not None
        if tree is None:
            tree = _tree(statement.source, self._parser.filename, self._flags, statement.start_line)
        scope = _ModuleScope()
        scope.visit(tree)
        if scope.globals:
            raise self._syntax_error(UnsupportedSyntaxError, "module-scope global declarations are unsupported across incremental compilation units", statement, scope.globals[0], shifted=shifted)
        future = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
        if future and not self._prologue:
            raise self._syntax_error(SyntaxError, "from __future__ imports must occur at the beginning of the stream", statement, future[0], shifted=shifted)
        docstring = bool(tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
        if docstring and not self._first:
            # Each code object otherwise treats its first string as a new module docstring.
            tree.body.insert(0, ast.copy_location(ast.Pass(), tree.body[0]))
        if not shifted:
            ast.increment_lineno(tree, statement.start_line - 1)
        try:
            code = compile(tree, self._parser.filename, "exec", flags=self._flags, dont_inherit=True)
        except SyntaxError as error:
            if error.lineno is not None:
                error.text = _source_line(statement.source, error.lineno - statement.start_line + 1)
                # AST compiler ranges are UTF-8 byte columns. Once source text
                # is attached, convert both endpoints for diagnostic renderers.
                error.offset = _character_column(error.text, error.offset)
                if error.end_lineno is not None:
                    end_text = _source_line(statement.source, error.end_lineno - statement.start_line + 1)
                    error.end_offset = _character_column(end_text, error.end_offset)
                error.args = (error.msg, (self._parser.filename, error.lineno, error.offset, error.text, error.end_lineno, error.end_offset))
            raise
        if sys.version_info >= (3, 14) and scope.annotations and not code.co_flags & __future__.annotations.compiler_flag:
            raise self._syntax_error(
                UnsupportedSyntaxError, "incremental module annotations require `from __future__ import annotations` on Python 3.14+", statement, scope.annotations[0], shifted=True
            )
        # Read feature requests from this unit, not code-object bits such as
        # CO_NESTED that overlap legacy future flags. Never borrow future flags
        # from later queued units accepted by the parser in the same feed.
        for future_node in future:
            for name in future_node.names:
                self._flags |= _FUTURES.get(name.name, 0)
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            if self._first and index == 0 and docstring:
                continue
            self._prologue = False
            break
        if tree.body:
            self._first = False
        return cast(CodeType, code)

    async def _run(self, statement: CodeUnit | BlockEvent) -> Execution | None:
        if isinstance(statement, BlockEvent):
            return await self._run_block(statement)
        started = monotonic()
        try:
            task = asyncio.current_task()
            cancellations = task.cancelling() if task is not None else 0
            # Deliver an already-pending cancellation before running user code.
            # A historical caught cancellation count alone does not poison a new operation.
            await asyncio.sleep(0)
            code = self._compile(statement)
            # eval returns the top-level-await coroutine; exec would discard it.
            result = eval(code, self.namespace, self.namespace)
            if inspect.isawaitable(result):
                await result
            # User code may catch cancellation. Do not start another unit while
            # the owning task still carries an outstanding cancellation request.
            if task is not None and task.cancelling() > cancellations:
                raise asyncio.CancelledError
        except BaseException:
            # The owning operation records terminal state; this layer identifies the attempted unit.
            self._failed_unit = statement
            raise
        event = Execution(len(self._events), statement, started, monotonic())
        self._events.append(event)
        return event

    async def _evaluate(self, code: CodeType) -> _Evaluation:
        try:
            result = eval(code, self.namespace, self.namespace)
            # Await only syntactic top-level await, never an ordinary expression
            # whose value happens to be an awaitable (e.g. a context manager).
            if code.co_flags & inspect.CO_COROUTINE:
                result = await result
        except BaseException as error:  # noqa: BLE001 - the enclosing streamed with must see the original exception.
            cause = error.__cause__
            # Python 3.11 wraps StopIteration at this artificial module-coroutine
            # boundary. A real user-coroutine wrapper has already passed through
            # our frame, so it must retain its native RuntimeError semantics.
            if code.co_flags & inspect.CO_COROUTINE and type(error) is RuntimeError and isinstance(cause, StopIteration) and _has_frame(cause, code) and not _has_frame(error, code):
                error = cause
            return _Evaluation(error=error)
        return _Evaluation(value=result)

    async def _close_frame(self, frame: _Frame, error: BaseException | None, *, fatal: bool = False) -> BaseException | None:
        task = asyncio.current_task()
        cancellations = task.cancelling() if task is not None else 0
        while frame.scopes:
            scope = frame.scopes.pop()
            suppressed = False
            try:
                remaining = await scope.close(error)
                suppressed = error is not None and remaining is None
                if not suppressed:
                    error = remaining
            except BaseException as replacement:  # noqa: BLE001 - outer exits must receive cleanup failures, including cancellation.
                error = replacement
            # A cleanup can swallow a newly delivered cancellation. Preserve it
            # before another manager could mistake this for normal completion.
            if task is not None and task.cancelling() > cancellations:
                cancellations = task.cancelling()
                fatal = True
                if suppressed or error is None:
                    cancellation = asyncio.CancelledError()
                    cancellation.__context__ = error
                    error = cancellation
            if suppressed and not fatal:
                error = None
        return error

    async def _unwind(self, error: BaseException, *, fatal: bool = False) -> BaseException | None:
        current: BaseException | None = error
        task = asyncio.current_task()
        cancellations = task.cancelling() if task is not None else 0
        # Keep structural frames until parser exits arrive, even after resources
        # close: a suppressed exception must still skip its unread suite.
        for frame in reversed(self._frames):
            frame.active = frame.parent_active = False
            current = await self._close_frame(frame, current, fatal=fatal)
            fatal |= task is not None and task.cancelling() > cancellations
            if current is None:
                # Suppression exits this with statement. Its unread body stays
                # inactive, while the enclosing suite may continue when it closes.
                return None
        return current

    async def _fail(self, error: BaseException) -> BaseException:
        # Source failure/cancellation is never successful EOF. Even a manager
        # that suppresses it cannot authorize more execution or a normal commit.
        remaining = await self._unwind(error, fatal=True)
        if remaining is not None:
            error = remaining
        self._frames.clear()
        self._owner = None
        self._error = error
        self._state = "failed"
        return error

    async def _run_block(self, item: BlockEvent) -> Execution | None:
        if item.kind == "enter" and item.depth == 0:
            self._started_block = item.unit.start
        started = monotonic()
        task = asyncio.current_task()
        cancellations = task.cancelling() if task is not None else 0
        try:
            await asyncio.sleep(0)
            node = item.node
            code = None
            bindings: list[tuple[CodeType, tuple[CodeType, int] | None]] = []
            if item.kind == "statement":
                assert isinstance(node, ast.Module)
                code = self._compile(item.unit, node)
            elif item.kind in {"enter", "branch"}:
                self._first = self._prologue = False
                if isinstance(node, ast.If):
                    code = compile(ast.Expression(node.test), self._parser.filename, "eval", flags=self._flags, dont_inherit=True)
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for context in node.items:
                        expression = compile(ast.Expression(context.context_expr), self._parser.filename, "eval", flags=self._flags, dont_inherit=True)
                        binding = compile_binding(context.optional_vars, self._parser.filename, self._flags) if context.optional_vars is not None else None
                        bindings.append((expression, binding))
            # Compile even inactive suites, but never let a with suppress a
            # compiler error. Only actual user-code execution is handled below.
        except BaseException:
            self._failed_unit = item.unit
            raise
        try:
            if item.kind == "enter":
                active = not self._frames or self._frames[-1].active
                frame = _Frame(started, active, active)
                self._frames.append(frame)
                self._owner = task
                if isinstance(node, ast.If):
                    assert code is not None
                    frame.active = frame.taken = active and bool((await self._evaluate(code)).unwrap())
                elif active:
                    for expression, binding in bindings:
                        manager = (await self._evaluate(expression)).unwrap()
                        scope, value = await Scope.open(manager, isinstance(node, ast.AsyncWith))
                        if scope is None:
                            reraise(value)
                        frame.scopes.append(scope)
                        if binding is not None:
                            assignment, index = binding
                            constants = list(assignment.co_consts)
                            constants[index] = value
                            (await self._evaluate(assignment.replace(co_consts=tuple(constants)))).unwrap()
            elif item.kind == "branch":
                frame = self._frames[-1]
                frame.active = frame.parent_active and not frame.taken and (code is None or bool((await self._evaluate(code)).unwrap()))
                frame.taken |= frame.active
            elif item.kind == "statement":
                assert code is not None
                if not self._frames or self._frames[-1].active:
                    (await self._evaluate(code)).unwrap()
            else:
                frame = self._frames.pop()
                started = frame.started
                error = await self._close_frame(frame, None)
                if error is not None:
                    reraise(error)
                if not self._frames:
                    self._owner = None
            if task is not None and task.cancelling() > cancellations:
                raise asyncio.CancelledError
        except BaseException as error:  # noqa: BLE001 - native with may suppress any user exception.
            self._failed_unit = item.unit
            fatal = task is not None and task.cancelling() > cancellations
            remaining = await self._unwind(error, fatal=fatal)
            if remaining is not None:
                reraise(remaining)
            if task is not None and task.cancelling() > cancellations:
                raise asyncio.CancelledError
            self._failed_unit = None
        if item.depth == 0 and item.kind in {"statement", "exit"}:
            self._started_block = None
            event = Execution(len(self._events), item.unit, started, monotonic())
            self._events.append(event)
            return event
        return None

    async def feed(self, delta: str) -> tuple[Execution, ...]:
        """Append new source and execute its sealed units in order.

        Return only this call's completed events. On failure, earlier effects
        remain; inspect ``events``, ``failed_unit``, ``queued`` and ``parser_state``.
        """
        self._enter()
        try:
            self._queued.extend(self._parser.feed(delta))
            events = []
            while self._queued:
                event = await self._run(self._queued.popleft())
                if event is not None:
                    events.append(event)
            return tuple(events)
        except BaseException as error:
            failure = await self._fail(error)
            if failure is not error:
                reraise(failure)
            raise
        finally:
            self._busy = False

    async def finish(self) -> tuple[Execution, ...]:
        """Declare successful EOF and execute the remaining source, then close.

        Never use as unconditional cleanup: this can execute a pending loop.
        Invalid EOF or execution failure is terminal and preserves earlier effects.
        """
        self._enter()
        try:
            self._queued.extend(self._parser.finish())
            events = []
            while self._queued:
                event = await self._run(self._queued.popleft())
                if event is not None:
                    events.append(event)
            self._state = "finished"
            return tuple(events)
        except BaseException as error:
            failure = await self._fail(error)
            if failure is not error:
                reraise(failure)
            raise
        finally:
            self._busy = False

    def abort(self) -> None:
        """Abandon pending source after a transport failure, retaining it for diagnostics."""
        if self._busy:
            raise RuntimeError("cancel the active operation before aborting")
        if any(frame.scopes for frame in self._frames):
            raise RuntimeError("open contexts require `await executor.aclose()`")
        if self._state == "open":
            self._frames.clear()
            self._owner = None
            self._state = "aborted"

    async def aclose(self) -> None:
        """Abandon source and await open context exits without declaring EOF."""
        if self._busy:
            raise RuntimeError("cancel the active operation before closing")
        if self._state != "open":
            return
        self._enter()
        try:
            task = asyncio.current_task()
            cancellations = task.cancelling() if task is not None else 0
            abandoned = GeneratorExit()
            error = await self._unwind(abandoned, fatal=True)
            if error is not abandoned and error is not None:
                reraise(error)
            if task is not None and task.cancelling() > cancellations:
                raise asyncio.CancelledError
            self._state = "aborted"
        except BaseException as error:
            self._error = error
            self._state = "failed"
            raise
        finally:
            self._frames.clear()
            self._owner = None
            self._busy = False

    async def stream(self, chunks: AsyncIterable[str]) -> AsyncGenerator[Execution, None]:
        """Execute a source iterator, retaining diagnostics on this executor.

        Normal exhaustion declares EOF. Adapters must raise for truncation or
        failed transport. Every exit attempts to await the acquired iterator’s
        ``aclose()`` when provided.
        This operation owns the executor until exhausted or explicitly closed.
        """
        self._enter()
        iterator = None
        yielding = False
        try:
            iterator = aiter(chunks)
            while True:
                self._check_owner()
                # With no open block, each anext may resume in a different task.
                task = asyncio.current_task()
                cancellations = task.cancelling() if task is not None else 0
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    if task is not None and task.cancelling() > cancellations:
                        raise asyncio.CancelledError
                    break
                if task is not None and task.cancelling() > cancellations:
                    raise asyncio.CancelledError
                self._queued.extend(self._parser.feed(chunk))
                if not self._queued:
                    # Buffered input can return immediately without suspending.
                    # Keep empty/unfinished streams cancellable between chunks.
                    await asyncio.sleep(0)
                while self._queued:
                    event = await self._run(self._queued.popleft())
                    if event is not None:
                        yielding = True
                        yield event
                        yielding = False
            self._queued.extend(self._parser.finish())
            while self._queued:
                event = await self._run(self._queued.popleft())
                if event is not None:
                    yielding = True
                    yield event
                    yielding = False
            self._state = "finished"
        except BaseException as error:
            # Only GeneratorExit injected at our event yield is consumer closure.
            # The same exception from a source or executed unit is a real failure.
            if not (yielding and isinstance(error, GeneratorExit)):
                failure = await self._fail(error)
                if failure is not error:
                    reraise(failure)
            raise
        finally:
            if self._state == "open":
                self._state = "aborted"
            try:
                cleanup_task = asyncio.current_task()
                cancellations = cleanup_task.cancelling() if cleanup_task is not None else 0
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
                if cleanup_task is not None and cleanup_task.cancelling() > cancellations:
                    raise asyncio.CancelledError
            except BaseException as cleanup_error:
                # The entry guard excludes stale errors from earlier terminal operations.
                if self._error is None or not isinstance(cleanup_error, Exception):
                    self._error = cleanup_error
                    self._state = "failed"
                    raise
                try:
                    BaseException.add_note(self._error, f"Source iterator cleanup also failed: {cleanup_error!r}")
                except Exception:  # noqa: BLE001, S110 - secondary diagnostics must not replace the primary error.
                    # A broken exception repr or notes attribute must not replace
                    # the execution failure merely while attaching diagnostics.
                    pass
                except BaseException as note_error:
                    self._error = note_error
                    raise
            finally:
                self._busy = False


async def incremental_exec(chunks: AsyncIterable[str], namespace: dict[str, Any] | None = None, *, filename: str = "<partial-python>", stream_blocks: Block = Block.MODULE) -> dict[str, Any]:
    """Execute validated source as it arrives and return the namespace used.

    Normal iterator exhaustion authorizes EOF. The source adapter must raise on
    truncation or failure. Use ``Executor.stream`` for events and diagnostics.
    """
    executor = Executor(namespace, filename=filename, stream_blocks=stream_blocks)
    async with aclosing(executor.stream(chunks)) as events:
        async for _ in events:
            pass
    return executor.namespace
