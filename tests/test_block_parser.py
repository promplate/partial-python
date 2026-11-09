"""Internal block events are proven by physical source, never temporary EOF."""

import ast

import pytest

from partial_python import ALL, ASYNC_WITH, IF, MODULE, WITH, Parser
from partial_python._blocks import BlockParser

PROGRAMS = [
    "first()\nsecond()\n",
    "if condition:\n    first()\n    second()\nelif other:\n    third()\nelse:\n    fourth()\nlast()\n",
    "if outer:\n    if inner:\n        first()\n    else:\n        second()\nelse:\n    third()\nlast()\n",
    "if condition: first(); second()\nelif other: third()\nelse: fourth()\nlast()\n",
    "if condition:\n    first()\nelif other: second(); third()\nelse: fourth()\n",
    "with first() as x, second() as y:\n    use(x, y)\nlast()\n",
    "async with first() as x, second() as y:\n    await use(x, y)\nlast()\n",
    "with (\n    first() as x,\n    second() as y,\n):\n    use(x, y)\n",
    "with first(): one(); two()\nnext()\n",
    "with first():\n    if test:\n        with second():\n            use()\n    else:\n        other()\nlast()\n",
    "if condition:\n    for value in values:\n        if nested:\n            use(value)\n    else:\n        done()\n    next()\nlast()\n",
    "with manager:\n    try:\n        use()\n    except ValueError:\n        recover()\n    finally:\n        clean()\n    next()\nlast()\n",
    "if condition:\n    @decorate\n    def f():\n        with manager:\n            use()\n    f()\nlast()\n",
    "if condition:\n    match value:\n        case [x, *xs]:\n            use(x, xs)\n        case _:\n            skip()\n    next()\nlast()\n",
    "if condition:\n    value = [\n        first(),\n        second(),\n    ]\n    use(value)\nlast()\n",
    "if condition:\n    value = '''first\nnot_a_dedent\nthird'''\n    use(value)\nlast()\n",
    'if condition:\n    value = f"hello {name!r:>10}"\n    use(value)\nlast()\n',
    "if condition:\n\tfirst()\n\tif other:\n\t\tsecond()\n\telse:\n\t\tthird()\nlast()\n",
    "if condition:\r\n    first()\r\nelse:\r\n    second()\r\nlast()\r\n",
    "if condition:\r    first()\relse:\r    second()\rlast()\r",
    "# before\nwith manager:\n    # body\n    first()\n# between\nlast()\n# tail",
    "with manager:\n    first()\n    \\\nsecond()\nlast()\n",
    "with manager:\n    \\\nfirst()\nlast()\n",
    "with manager:\n\\\n    first()\nlast()\n",
    "with outer:\n    with inner:\n        first()\n\\\n    second()\nlast()\n",
    "with outer:\n    with inner:\n        first()\n\\\n    \\\nsecond()\nlast()\n",
    "if condition:\n    first()\n\\\nelse:\n    second()\nlast()\n",
    "with manager:\n    变量 = 'é🙂'\n    if 变量:\n        使用(变量)\nlast()",
    "# only a comment\n",
    "\n\n",
    "",
]


def signature(event):
    return event.kind, event.unit, event.depth, ast.dump(event.node, include_attributes=True) if event.node else None


def parse(chunks, flags=ALL):
    parser = BlockParser(stream_blocks=flags)
    events = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events


@pytest.mark.parametrize("source", PROGRAMS)
def test_partition_independent_events_and_original_top_level_units(source):
    expected = [signature(event) for event in parse([source])]
    for chunks in [source, *([source[:split], source[split:]] for split in range(len(source) + 1))]:
        actual = parse(chunks)
        assert [signature(event) for event in actual] == expected, chunks
        units = [event.unit for event in actual if event.depth == 0 and event.kind in {"statement", "exit"}]
        assert "".join(unit.source for unit in units) == source
        assert all(source[event.unit.start : event.unit.end] == event.unit.source for event in actual)


def test_header_and_body_are_early_but_only_real_dedent_closes_the_unit():
    parser = BlockParser(stream_blocks=ALL)
    header = "with manager:\n"
    (enter,) = parser.feed(header)
    assert enter.kind == "enter"
    assert isinstance(enter.node, ast.With) and enter.node.body == []
    (body,) = parser.feed("    first()\n")
    assert body.kind == "statement" and body.depth == 1
    assert parser.committed == 0
    assert parser.pending == header + body.unit.source
    assert parser.feed("\n# comment\n") == ()
    (close,) = parser.feed("n")
    assert close.kind == "exit"
    assert close.unit.source == header + body.unit.source
    assert parser.committed == close.unit.end
    assert parser.pending == "\n# comment\nn"
    parser.feed("ext()\n")
    parser.finish()


@pytest.mark.parametrize("word", ["e", "el", "eli", "elif", "els", "else"])
def test_partial_continuation_keyword_never_closes_if(word):
    parser = BlockParser(stream_blocks=ALL)
    parser.feed("if first:\n    one()\n")
    assert parser.feed(word) == ()
    assert parser.committed == 0


def test_branch_headers_keep_one_frame_and_original_expression_locations():
    source = "if first:\n    one()\nelif second:\n    two()\nelse:\n    three()\nafter()\n"
    events = parse(source)
    enter = next(event for event in events if event.kind == "enter")
    branches = [event for event in events if event.kind == "branch"]
    close = next(event for event in events if event.kind == "exit")
    assert [event.unit.source.split()[0] for event in branches] == ["elif", "else:"]
    assert all(event.depth == enter.depth for event in [*branches, close])
    assert isinstance(branches[0].node, ast.If)
    assert (branches[0].node.test.lineno, branches[0].node.test.col_offset) == (3, 5)
    assert branches[0].node.end_lineno == 3
    assert branches[1].node is None


def test_multiple_context_items_and_ast_locations_are_untouched():
    source = "if enabled:\n    with first() as x, second(x) as y:\n        use(x, y)\nlast()\n"
    events = parse(source)
    with_event = next(event for event in events if isinstance(event.node, ast.With))
    original = ast.parse(source).body[0].body[0]
    assert ast.dump(ast.Module(body=[item.context_expr for item in with_event.node.items]), include_attributes=True) == ast.dump(
        ast.Module(body=[item.context_expr for item in original.items]), include_attributes=True
    )
    use = next(event.node.body[0] for event in events if event.kind == "statement" and event.depth == 2)
    assert ast.dump(use, include_attributes=True) == ast.dump(original.body[0], include_attributes=True)


@pytest.mark.parametrize("flags, expanded", [(MODULE, []), (MODULE | IF, [ast.If]), (MODULE | WITH, [ast.With]), (MODULE | ASYNC_WITH, [ast.AsyncWith]), (ALL, [ast.If, ast.With, ast.AsyncWith])])
def test_only_selected_bodies_expand(flags, expanded):
    source = "if yes:\n    first()\nwith manager:\n    second()\nasync with manager:\n    third()\nlast()\n"
    assert [type(event.node) for event in parse(source, flags) if event.kind == "enter"] == expanded


@pytest.mark.parametrize(
    "compound",
    [
        "for x in xs:\n    with manager:\n        use(x)\n",
        "while condition:\n    if test:\n        use()\n",
        "try:\n    with manager:\n        use()\nfinally:\n    clean()\n",
        "def function():\n    if condition:\n        use()\n",
        "class C:\n    if condition:\n        attribute = 1\n",
        "match value:\n    case _:\n        with manager:\n            use()\n",
    ],
)
def test_unsupported_compounds_are_opaque_even_when_their_children_are_enabled(compound):
    parser = BlockParser(stream_blocks=ALL)
    assert parser.feed(compound) == ()
    (event,) = parser.feed("n")
    assert event.kind == "statement" and event.unit.source == compound
    assert isinstance(event.node, ast.Module)
    parser.feed("ext()\n")
    parser.finish()


@pytest.mark.parametrize(
    "source", ["if condition:\n", "with manager:\n    value = (\n", "with manager:\n    value = '''open", "if condition:\n    one()\nelse:\n", "if condition:\n    one()\n  invalid()\n"]
)
def test_invalid_eof_does_not_invent_exit_events(source):
    parser = BlockParser(stream_blocks=ALL)
    parser.feed(source)
    with pytest.raises((SyntaxError, ValueError)):
        parser.finish()
    assert parser.closed and parser.committed == 0
    with pytest.raises(RuntimeError):
        parser.feed("later()\n")


def test_default_parser_still_returns_only_complete_original_units():
    parser = Parser()
    source = "with manager:\n    one()\n    two()\n"
    assert parser.feed(source) == ()
    (unit,) = parser.feed("n")
    assert unit.source == source


def test_lifecycle_and_type_checking():
    parser = BlockParser()
    with pytest.raises(TypeError):
        parser.feed(b"code")
    assert parser.feed("") == ()
    assert parser.finish() == ()
    with pytest.raises(RuntimeError):
        parser.finish()


class _DifferentialContext:
    def __init__(self, log, name, value=None, suppress=False):
        self.log, self.name, self.value, self.suppress = log, name, value, suppress

    def __enter__(self):
        self.log.append(("enter", self.name))
        return self.value if self.value is not None else self.name

    def __exit__(self, kind, _error, _traceback):
        self.log.append(("exit", self.name, kind.__name__ if kind else None))
        return self.suppress

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *error):
        return self.__exit__(*error)


EXECUTION_PROGRAMS = [
    "if check('if', first):\n    with ctx('outer') as name:\n        log.append(name)\n        if check('nested', second): log.append('nested')\n        else: log.append('other')\nelif check('elif', second):\n    log.append('elif')\nelse:\n    log.append('else')\nlog.append('end')\n",
    "with ctx('outer'):\n\ttext = '''literal\nelse:\nwith untouched:\n'''\n\tif first:\n\t\tlog.append(text)\n\telse:\n\t\tlog.append('else')\nlog.append('end')\n",
    "with ctx('outer'):\r\n    values = [\r\n        1,\r\n        2,\r\n    ]\r\n    log.append(f'值:{values!r}')\r\nlog.append('end')\r\n",
    "if first:\n    for value in [1, 2]:\n        with ctx(str(value)):\n            log.append(value)\n    else:\n        log.append('loop-else')\n    log.append('after-loop')\nlog.append('end')\n",
    "with ctx('outer'):\n    try:\n        if first:\n            raise ValueError('expected')\n        log.append('try')\n    except ValueError:\n        log.append('except')\n    finally:\n        log.append('finally')\n    log.append('after-try')\nlog.append('end')\n",
    "with ctx('outer'):\n    match [first, second]:\n        case [True, value] if check('guard', value):\n            log.append('matched')\n        case _:\n            log.append('fallback')\n    log.append('after-match')\nlog.append('end')\n",
    "if first:\n    def function():\n        with ctx('function'):\n            return 42\n    log.append(function())\nlog.append('end')\n",
    "async with ctx('first') as x, ctx('second') as y:\n    await record((x, y))\n    if first:\n        async with ctx('inner'):\n            await record('inner')\n    else:\n        await record('else')\nlog.append('end')\n",
    "with ctx('unpack', (1, 2)) as (x, y):\n    log.append((x, y))\n    with ctx('suppress', suppress=True):\n        log.append('before-error')\n        raise ValueError('suppressed')\n        log.append('unreachable')\n    log.append('after-suppressed')\nlog.append('end')\n",
    "with ctx('outer'):\n    with ctx('inner'):\n        log.append('inner')\n\\\n    \\\nlog.append('outer')\nlog.append('end')\n",
]


@pytest.mark.parametrize("source", EXECUTION_PROGRAMS)
@pytest.mark.parametrize("first,second", [(True, True), (False, True), (False, False)])
@pytest.mark.parametrize("partition", ["whole", "characters", "seven"])
async def test_original_block_ast_matches_whole_program_execution(source, first, second, partition):
    import inspect

    from partial_python import Executor

    def namespace():
        log = []

        def check(label, value):
            log.append(("check", label))
            return value

        async def record(value):
            log.append(value)

        return {"log": log, "first": first, "second": second, "check": check, "record": record, "ctx": lambda *args, **kwargs: _DifferentialContext(log, *args, **kwargs)}

    expected, actual = namespace(), namespace()
    result = eval(compile(source, "differential.py", "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), expected, expected)
    if inspect.isawaitable(result):
        await result
    chunks = [source] if partition == "whole" else source if partition == "characters" else [source[start : start + 7] for start in range(0, len(source), 7)]
    executor = Executor(actual, filename="differential.py", stream_blocks=ALL)
    for chunk in chunks:
        await executor.feed(chunk)
    await executor.finish()
    assert actual["log"] == expected["log"]
    assert "".join(event.unit.source for event in executor.events) == source
    assert executor.parser_state.committed == len(source)
    assert executor.parser_state.pending == ""


@pytest.mark.parametrize("tail", ["    broken = (\n", "    broken = '''open\n", "    broken = \0\n", "\0", "\\"])
async def test_invalid_suffix_never_repairs_or_completes_an_open_context(tail):
    from partial_python import Executor

    log = []
    executor = Executor({"log": log, "ctx": _DifferentialContext(log, "scope")}, filename="broken.py", stream_blocks=ALL)
    prefix = "with ctx:\n    log.append('stable')\n"
    assert await executor.feed(prefix) == ()
    assert log == [("enter", "scope"), "stable"]
    assert await executor.feed(tail) == ()
    assert executor.parser_state.pending == prefix + tail
    assert executor.parser_state.committed == 0 and executor.events == ()
    with pytest.raises((SyntaxError, ValueError)):
        await executor.finish()
    assert executor.state == "failed"
    assert executor.parser_state.closed
    assert executor.parser_state.pending == prefix + tail
    assert log[:2] == [("enter", "scope"), "stable"]
    assert len(log) == 3 and log[-1][:2] == ("exit", "scope")
    assert executor.events == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("partition", ["whole", "characters"])
async def test_failed_statement_uses_exact_original_source_and_absolute_traceback_line(newline, partition):
    import traceback

    from partial_python import Executor

    log = []
    lines = ["log.append('root')", "with ctx:", "    变量 = 'é🙂'", "    missing_name()", "    log.append('never')", "log.append('after')"]
    source = newline.join(lines) + newline
    executor = Executor({"log": log, "ctx": _DifferentialContext(log, "scope")}, filename="failure.py", stream_blocks=ALL)
    with pytest.raises(NameError) as caught:
        for chunk in [source] if partition == "whole" else source:
            await executor.feed(chunk)
        await executor.finish()
    assert log == ["root", ("enter", "scope"), ("exit", "scope", "NameError")]
    unit = executor.failed_unit
    assert unit is not None
    assert unit.source == lines[3] + newline
    assert source[unit.start : unit.end] == unit.source
    assert (unit.start_line, unit.end_line) == (4, 5)
    assert [frame.lineno for frame in traceback.extract_tb(caught.value.__traceback__) if frame.filename == "failure.py"] == [4]
    assert [event.unit.source for event in executor.events] == [lines[0] + newline]


@pytest.mark.parametrize("shape", ["literal", "multiline-string", "brackets", "opaque", "comment"])
def test_streaming_scanner_work_grows_with_new_source(monkeypatch, shape):
    import tokenize

    import partial_python._blocks as blocks
    import partial_python.parser as ordinary

    generate = tokenize.generate_tokens
    tree = ordinary._tree
    scanned = parsed = 0

    def measured_tokens(readline):
        def measured_read():
            nonlocal scanned
            line = readline()
            scanned += len(line)
            return line

        return generate(measured_read)

    def measured_tree(source, *args, **kwargs):
        nonlocal parsed
        parsed += len(source)
        return tree(source, *args, **kwargs)

    monkeypatch.setattr(tokenize, "generate_tokens", measured_tokens)
    monkeypatch.setattr(ordinary, "_tree", measured_tree)
    monkeypatch.setattr(blocks, "_tree", measured_tree)
    totals = []
    for count in [256, 1024]:
        source = {
            "literal": "with manager:\n    '" + "a" * count + "'\n",
            "multiline-string": "with manager:\n    text = '''\n" + "line\n" * count + "'''\n",
            "brackets": "with manager:\n    values = [\n" + "        1,\n" * count + "    ]\n",
            "opaque": "with manager:\n    for value in []:\n" + "        use(value)\n" * count + "    next()\n",
            "comment": "with manager:\n    #" + "a" * count + "\n    pass\n",
        }[shape]
        scanned = parsed = 0
        parse(source)
        totals.append(scanned + parsed)
        assert scanned + parsed < 60 * len(source)
    # Count actual tokenizer input and compilation spans, not elapsed time. A
    # fourfold input must not regress to reparsing each complete prefix again.
    assert totals[1] < 6 * totals[0]
