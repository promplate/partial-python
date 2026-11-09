"""Explicit scopes for incremental execution; independent of display completion."""

from enum import Flag, auto


class Block(Flag):
    """Select which statement bodies may execute before their enclosing source ends."""

    NONE = 0
    MODULE = auto()
    IF = auto()
    WITH = auto()
    ASYNC_WITH = auto()
    ALL = MODULE | IF | WITH | ASYNC_WITH


NONE = Block.NONE
MODULE = Block.MODULE
IF = Block.IF
WITH = Block.WITH
ASYNC_WITH = Block.ASYNC_WITH
ALL = Block.ALL

__all__ = ["ALL", "ASYNC_WITH", "IF", "MODULE", "NONE", "WITH", "Block"]
