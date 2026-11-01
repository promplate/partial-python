"""Count parser work rather than asserting machine-dependent wall-clock timings."""

import pytest

import partial_python.parser as parser_module
from partial_python import Parser


def measure_work(monkeypatch, source, chunk_size):
    counts = {"normalized": 0, "parsed": 0}
    original_normalize = parser_module._normalize
    original_tree = parser_module._tree

    def normalize(text):
        counts["normalized"] += len(text)
        return original_normalize(text)

    def tree(text, *args, **kwargs):
        counts["parsed"] += len(text)
        return original_tree(text, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(parser_module, "_normalize", normalize)
        patch.setattr(parser_module, "_tree", tree)
        parser = Parser()
        statements = []
        for start in range(0, len(source), chunk_size):
            statements.extend(parser.feed(source[start : start + chunk_size]))
        statements.extend(parser.finish())
    assert "".join(statement.source for statement in statements) == source
    return counts


@pytest.mark.parametrize("chunk_size", [1, 17, 1_000_000])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_growing_suite_scanning_scales_with_source_length(monkeypatch, chunk_size, newline):
    def source(lines):
        return newline.join(["for item in values:", *["    visit(item)"] * lines, "complete()", ""])

    small = source(64)
    large = source(256)
    small_work = measure_work(monkeypatch, small, chunk_size)
    large_work = measure_work(monkeypatch, large, chunk_size)
    # Four times as many bounded logical lines must not entail quadratic rescans,
    # including when the entire source arrives in one transport chunk.
    for operation in small_work:
        assert large_work[operation] <= small_work[operation] * 5
    assert large_work["normalized"] < len(large) * 30
    assert large_work["parsed"] < len(large) * 5


def test_trivia_prefix_is_not_reparsed_for_every_expression_character(monkeypatch):
    source = "# retained source comment\n" * 100 + "value = (" + "1 + " * 100 + "1)\n"
    work = measure_work(monkeypatch, source, 1)
    # Retaining original comments must not multiply AST work by the length of
    # the following unfinished logical line. Lexical long-line cost is separate.
    assert work["parsed"] < len(source) * 5
