"""Native context-manager lifetimes suspended between source fragments."""

import ast
from collections.abc import AsyncGenerator
from types import CodeType
from typing import Any, NoReturn, Self, cast


def reraise(error: BaseException) -> NoReturn:
    """Propagate cleanup failures without replacing their native exception chain."""
    context = error.__context__
    try:
        raise error
    except BaseException:
        error.__context__ = context
        raise


async def _drive(manager: Any, asynchronous: bool) -> AsyncGenerator[tuple[bool, Any], BaseException | None]:
    entered = requested = False
    outcome = None
    # Recreate the handled-exception state inside the native suite, preserving
    # the body traceback and chain. Keep both branches inline: a helper call
    # would add a driver frame to the traceback seen by the context manager.
    try:
        if asynchronous:
            async with manager as value:
                entered = True
                error = yield True, value
                requested = True
                if error is not None:
                    traceback, context = error.__traceback__, error.__context__
                    try:
                        raise error
                    except BaseException:
                        error.__traceback__, error.__context__ = traceback, context
                        raise
        else:
            with manager as value:
                entered = True
                error = yield True, value
                requested = True
                if error is not None:
                    traceback, context = error.__traceback__, error.__context__
                    try:
                        raise error
                    except BaseException:
                        error.__traceback__, error.__context__ = traceback, context
                        raise
    except BaseException as error:
        # Actual generator closure must never yield, even when a manager replaces
        # GeneratorExit. Explicit body errors instead travel as values below.
        if entered and not requested:
            raise
        outcome = error
    if not entered or requested:
        # Transport exceptions as data: crossing an async-generator boundary can
        # otherwise wrap StopIteration/StopAsyncIteration or consume GeneratorExit.
        yield False, outcome


class Scope:
    def __init__(self, driver: AsyncGenerator[tuple[bool, Any], BaseException | None]):
        self._driver: AsyncGenerator[tuple[bool, Any], BaseException | None] | None = driver

    @classmethod
    async def open(cls, manager: Any, asynchronous: bool) -> tuple[Self | None, Any]:
        driver = _drive(manager, asynchronous)
        entered, value = await anext(driver)
        if not entered:
            await driver.aclose()
            return None, value
        return cls(driver), value

    async def close(self, error: BaseException | None = None) -> BaseException | None:
        """Exit once and return the remaining error, avoiding coroutine conversion."""
        driver, self._driver = self._driver, None
        if driver is None:
            return error
        try:
            _, outcome = await driver.asend(error)
        finally:
            await driver.aclose()
        return cast(BaseException | None, outcome)


def compile_binding(target: ast.expr, filename: str, flags: int) -> tuple[CodeType, int]:
    """Compile an as-target without introducing a visible temporary name."""
    # NaN is non-reflexive, so compiler constant pooling cannot merge this fresh
    # marker with any target literal, including another constant-folded NaN.
    marker = float("nan")
    statement = ast.copy_location(ast.Assign(targets=[target], value=ast.copy_location(ast.Constant(marker), target)), target)
    tree = ast.fix_missing_locations(ast.Module(body=[statement], type_ignores=[]))
    code = cast(CodeType, compile(tree, filename, "exec", flags=flags, dont_inherit=True))
    indices = [index for index, constant in enumerate(code.co_consts) if constant is marker]
    if len(indices) != 1:
        raise RuntimeError("compiler did not preserve the context binding marker")
    return code, indices[0]
