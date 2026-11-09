from enum import Flag

import pytest

import partial_python
from partial_python import blocks
from partial_python.blocks import ALL, ASYNC_WITH, IF, MODULE, NONE, WITH, Block
from partial_python.preview import Allow


@pytest.mark.parametrize("name", ["NONE", "MODULE", "IF", "WITH", "ASYNC_WITH", "ALL"])
def test_block_aliases_are_identical_at_both_import_paths(name):
    member = getattr(Block, name)
    assert getattr(blocks, name) is member
    assert getattr(partial_python, name) is member
    assert name in blocks.__all__ and name in partial_python.__all__


def test_block_type_is_public_at_both_import_paths():
    assert partial_python.Block is blocks.Block is Block
    assert "Block" in blocks.__all__ and "Block" in partial_python.__all__


def test_all_contains_exactly_the_implemented_independent_scopes():
    assert ALL == MODULE | IF | WITH | ASYNC_WITH
    assert set(Block) == {MODULE, IF, WITH, ASYNC_WITH}
    assert set(Block.__members__) == {"NONE", "MODULE", "IF", "WITH", "ASYNC_WITH", "ALL"}
    assert NONE.value == 0
    assert not NONE and ALL
    assert ALL & ~MODULE == IF | WITH | ASYNC_WITH


def test_block_options_do_not_enable_other_scopes_implicitly():
    assert (IF | WITH) & MODULE == NONE
    assert (IF | WITH) & ASYNC_WITH == NONE
    assert MODULE & IF == NONE


@pytest.mark.parametrize("foreign", [0, 1, False, True, Allow.NONE, Allow.TAIL, Allow.ALL])
def test_execution_options_do_not_mix_with_integers_or_preview_options(foreign):
    assert isinstance(MODULE, Flag) and not isinstance(MODULE, int)
    with pytest.raises(TypeError):
        MODULE | foreign
    with pytest.raises(TypeError):
        foreign | MODULE


def test_unknown_block_bits_are_rejected():
    with pytest.raises(ValueError):
        Block(1 << 4)
