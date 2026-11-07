"""Independent syntax assertions and structural parser-work measurements."""

import ast
from contextlib import contextmanager

import partial_python.parser as parser_module
from partial_python import Parser


def ast_nodes(source, start_line=1):
    module = compile(source, "<partition-test>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    ast.increment_lineno(module, start_line - 1)
    return [ast.dump(node, include_attributes=True) for node in module.body]


def assert_partition(source, chunks):
    expected = ast_nodes(source)
    parser = Parser()
    units = []
    actual = []
    received = committed = 0
    line = 1

    def inspect(batch):
        nonlocal committed, line
        for unit in batch:
            assert unit.start == committed
            assert unit.end == unit.start + len(unit.source) <= received
            assert unit.source == source[unit.start : unit.end]
            assert unit.start_line == line
            line += unit.source.count("\n") + unit.source.count("\r") - unit.source.count("\r\n")
            assert unit.end_line == line
            committed = unit.end
            actual.extend(ast_nodes(unit.source, unit.start_line))
            assert actual == expected[: len(actual)]
            units.append(unit)

    for chunk in chunks:
        received += len(chunk)
        inspect(parser.feed(chunk))
        assert parser.committed == committed
        assert parser.pending == source[committed:received]
    inspect(parser.finish())
    assert committed == len(source)
    assert parser.pending == ""
    assert parser.closed
    assert actual == expected
    return units


@contextmanager
def count_work(monkeypatch):
    counts = {"normalized": 0, "parsed": 0, "tokenized": 0}
    original_normalize = parser_module._normalize
    original_tree = parser_module._tree
    original_tokens = parser_module.tokenize.generate_tokens

    def normalize(text):
        counts["normalized"] += len(text)
        return original_normalize(text)

    def tree(text, *args, **kwargs):
        counts["parsed"] += len(text)
        return original_tree(text, *args, **kwargs)

    def tokens(readline):
        def read():
            text = readline()
            counts["tokenized"] += len(text)
            return text

        return original_tokens(read)

    with monkeypatch.context() as patch:
        patch.setattr(parser_module, "_normalize", normalize)
        patch.setattr(parser_module, "_tree", tree)
        patch.setattr(parser_module.tokenize, "generate_tokens", tokens)
        yield counts
