"""Sequential execution of original source under explicit interactive semantics."""

import __future__

import ast
import asyncio
import inspect
import sys
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass
from time import monotonic
from types import CodeType
from typing import Any, Literal, cast

from .parser import _FUTURES, CodeUnit, Parser, _source_line, _tree


def _character_column(text: str, byte_column: int | None) -> int | None:
    if byte_column is None or byte_column <= 0:
        return byte_column
    return len(text.encode()[: byte_column - 1].decode(errors="replace")) + 1


ExecutionState = Literal["open", "finished", "failed", "aborted"]


class UnsupportedSyntaxError(SyntaxError):
    """Syntax whose cross-unit semantics cannot be preserved by this executor."""


@dataclass(frozen=True, slots=True)
class ParserState:
    pending: str
    committed: int
    closed: bool


@dataclass(frozen=True, slots=True)
class Execution:
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


class Executor:
    """One append-only execution stream sharing a caller-owned global namespace.

    Failure and cancellation are terminal: earlier effects remain, and accepted
    statements are never retried. This is an in-process executor, not a sandbox.
    Concurrent or recursive writes are rejected rather than silently reordered.
    """

    def __init__(self, namespace: dict[str, Any] | None = None, *, filename: str = "<partial-python>"):
        self._namespace = namespace if namespace is not None else {"__name__": "__main__"}
        self._parser = Parser(filename=filename)
        self._events: list[Execution] = []
        self._flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        self._prologue = True
        self._first = True
        self._busy = False
        self._state: ExecutionState = "open"
        self._failed_unit: CodeUnit | None = None
        self._error: BaseException | None = None
        self._unexecuted: deque[CodeUnit] = deque()

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
    def parser(self) -> ParserState:
        """Read-only diagnostics; accepting text directly would bypass the execution ledger."""
        return ParserState(self._parser.pending, self._parser.committed, self._parser.closed)

    @property
    def unexecuted(self) -> tuple[CodeUnit, ...]:
        return tuple(self._unexecuted)

    @property
    def events(self) -> tuple[Execution, ...]:
        return tuple(self._events)

    @property
    def state(self) -> ExecutionState:
        return self._state

    def _enter(self) -> None:
        if self._busy:
            raise RuntimeError("executor already has an active operation")
        if self._state != "open":
            raise RuntimeError(f"executor is {self._state}")
        self._busy = True

    def _syntax_error(self, error_type: type[SyntaxError], message: str, statement: CodeUnit, node: ast.stmt, *, shifted: bool = False) -> SyntaxError:
        line = node.lineno - (statement.start_line - 1 if shifted else 0)
        text = _source_line(statement.source, line)
        column = _character_column(text, node.col_offset + 1)
        return error_type(message, (self._parser.filename, statement.start_line + line - 1, column, text))

    def _compile(self, statement: CodeUnit) -> CodeType:
        tree = _tree(statement.source, self._parser.filename, self._flags, statement.start_line)
        scope = _ModuleScope()
        scope.visit(tree)
        if scope.globals:
            raise self._syntax_error(UnsupportedSyntaxError, "module-scope global declarations are unsupported across incremental compilation units", statement, scope.globals[0])
        future = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
        if future and not self._prologue:
            raise self._syntax_error(SyntaxError, "from __future__ imports must occur at the beginning of the stream", statement, future[0])
        docstring = bool(tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
        if docstring and not self._first:
            # Each code object otherwise treats its first string as a new module docstring.
            tree.body.insert(0, ast.copy_location(ast.Pass(), tree.body[0]))
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

    async def _run(self, statement: CodeUnit) -> Execution:
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
        except BaseException as error:
            self._error = error
            self._failed_unit = statement
            self._state = "failed"
            raise
        event = Execution(len(self._events), statement, started, monotonic())
        self._events.append(event)
        return event

    async def feed(self, delta: str) -> tuple[Execution, ...]:
        self._enter()
        try:
            self._unexecuted.extend(self._parser.feed(delta))
            events = []
            while self._unexecuted:
                events.append(await self._run(self._unexecuted.popleft()))
            return tuple(events)
        except BaseException as error:
            self._error = error
            self._state = "failed"
            raise
        finally:
            self._busy = False

    async def finish(self) -> tuple[Execution, ...]:
        self._enter()
        try:
            self._unexecuted.extend(self._parser.finish())
            events = []
            while self._unexecuted:
                events.append(await self._run(self._unexecuted.popleft()))
            self._state = "finished"
            return tuple(events)
        except BaseException as error:
            self._error = error
            self._state = "failed"
            raise
        finally:
            self._busy = False

    def abort(self) -> None:
        """Abandon pending source after a transport failure, retaining it for diagnostics."""
        if self._busy:
            raise RuntimeError("cancel the active operation before aborting")
        if self._state == "open":
            self._state = "aborted"

    async def stream(self, chunks: AsyncIterable[str]) -> AsyncGenerator[Execution, None]:
        """Execute a source iterator, retaining diagnostics on this executor.

        Normal exhaustion declares EOF. Adapters must raise for truncation or
        failed transport. The acquired iterator is closed on every exit path.
        This operation owns the executor until exhausted or explicitly closed.
        """
        self._enter()
        iterator = None
        primary_error = None
        try:
            iterator = aiter(chunks)
            while True:
                # Each anext may be resumed by a different task (e.g. wait_for).
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
                self._unexecuted.extend(self._parser.feed(chunk))
                while self._unexecuted:
                    yield await self._run(self._unexecuted.popleft())
            self._unexecuted.extend(self._parser.finish())
            while self._unexecuted:
                yield await self._run(self._unexecuted.popleft())
            self._state = "finished"
        except GeneratorExit:
            raise
        except BaseException as error:
            primary_error = error
            self._error = error
            self._state = "failed"
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
                if primary_error is None or not isinstance(cleanup_error, Exception):
                    self._error = cleanup_error
                    self._state = "failed"
                    raise
                primary_error.add_note(f"Source iterator cleanup also failed: {cleanup_error!r}")
            finally:
                self._busy = False


def incremental_exec(chunks: AsyncIterable[str], namespace: dict[str, Any] | None = None, *, filename: str = "<partial-python>") -> AsyncGenerator[Execution, None]:
    """Return an execution stream; use Executor.stream when diagnostics are needed."""
    return Executor(namespace, filename=filename).stream(chunks)
