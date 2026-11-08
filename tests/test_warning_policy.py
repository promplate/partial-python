"""Compiler warning policy belongs to the host and must not be silenced."""

import sys
import warnings

import pytest

from partial_python import Executor


async def test_broad_warning_as_error_remains_terminal_without_executing_the_tail():
    category = SyntaxWarning if sys.version_info >= (3, 12) else DeprecationWarning
    executor = Executor(filename="<warning-policy>")
    await executor.feed("first = 1\nsecond = 2\n")
    tail = "value = '" + chr(92) + "z'\n"
    with warnings.catch_warnings():
        warnings.simplefilter("error", category)
        assert await executor.feed(tail) == ()
        with pytest.raises(SyntaxError) as caught:
            await executor.finish()
    assert executor.state == "failed" and executor.error is caught.value
    assert caught.value.lineno == 3
    assert executor.namespace["first"] == 1 and "value" not in executor.namespace
    assert executor.parser_state.pending == tail
