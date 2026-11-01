"""Read-only scanner conformance over real Python source; never executes corpus code."""

import __future__

import argparse
import ast
import hashlib
import json
import os
import random
import sys
import sysconfig
import tokenize
from pathlib import Path
from time import perf_counter

# The dev tool must inspect this checkout even if another version is installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from partial_python import Parser

SKIP_DIRS = {"site-packages", "dist-packages", "__pycache__", "node_modules", "venv", ".venv"}
FUTURES = {name: getattr(__future__, name).compiler_flag for name in __future__.all_feature_names}
AST_FLAGS = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


class Mismatch(Exception):
    def __init__(self, invariant, expected, actual):
        self.detail = {"invariant": invariant, "expected": str(expected)[:500], "actual": str(actual)[:500]}
        super().__init__(invariant)


def require(condition, invariant, expected, actual):
    if not condition:
        raise Mismatch(invariant, expected, actual)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def candidates(root):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix == ".py" and not path.is_symlink():
                yield path


def tree(source, filename, flags=0):
    return compile(source, filename, "exec", flags=AST_FLAGS | flags, dont_inherit=True)


def signature(node):
    return ast.dump(node, include_attributes=False)


def new_future_flags(module):
    flags = 0
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                flags |= FUTURES.get(alias.name, 0)
    return flags


def chunk_ends(length, mode, chunk_max, seed):
    if mode == "bulk":
        yield length
    elif mode == "characters":
        yield from range(1, length + 1)
    else:
        rng = random.Random(seed)
        offset = 0
        while offset < length:
            offset = min(length, offset + rng.randint(1, chunk_max))
            yield offset


def check_partition(source, path, expected, mode, chunk_max, seed, reference=None):
    parser = Parser(filename=str(path))
    committed = received = node_index = flags = 0
    line = 1
    units = []
    context = {"mode": mode, "file_seed": seed, "stage": "feed", "chunk_index": 0, "chunk_start": 0, "chunk_end": 0}

    def check_units(batch):
        nonlocal committed, node_index, flags, line
        for unit in batch:
            context["unit_start"] = unit.start
            context["unit_end"] = unit.end
            require(bool(unit.source), "nonempty unit", "at least one source character", unit.source)
            require(unit.start == committed, "contiguous source offsets", committed, unit.start)
            require(unit.end == unit.start + len(unit.source), "exact source length", unit.start + len(unit.source), unit.end)
            require(unit.end <= received, "unit cannot exceed received source", received, unit.end)
            require(unit.source == source[unit.start : unit.end], "original source slice", source[unit.start : unit.end], unit.source)
            end_line = line + unit.source.count("\n") + unit.source.count("\r") - unit.source.count("\r\n")
            require((unit.start_line, unit.end_line) == (line, end_line), "physical line span", (line, end_line), (unit.start_line, unit.end_line))
            context["stage"] = "unit_ast"
            module = tree(unit.source, str(path), flags)
            for node in module.body:
                actual = signature(node)
                require(node_index < len(expected), "AST prefix length", len(expected), node_index + 1)
                require(actual == expected[node_index], "unchanged AST prefix", expected[node_index], actual)
                node_index += 1
            flags |= new_future_flags(module)
            units.append((unit.start, unit.end, unit.start_line, unit.end_line))
            committed, line = unit.end, end_line

    try:
        for index, end in enumerate(chunk_ends(len(source), mode, chunk_max, seed)):
            context.update(stage="feed", chunk_index=index, chunk_start=received, chunk_end=end)
            context.pop("unit_start", None)
            context.pop("unit_end", None)
            chunk = source[received:end]
            received = end
            check_units(parser.feed(chunk))
            context["stage"] = "feed_state"
            require(parser.committed == committed, "committed cursor", committed, parser.committed)
            require(parser.pending == source[committed:received], "lossless pending source", source[committed:received], parser.pending)
            require(not parser.closed, "feed lifecycle", False, parser.closed)
        context["stage"] = "finish"
        context.pop("unit_start", None)
        context.pop("unit_end", None)
        check_units(parser.finish())
        context["stage"] = "final_state"
        require(parser.closed, "finish lifecycle", True, parser.closed)
        require(parser.pending == "", "empty finished buffer", "", parser.pending)
        require(committed == parser.committed == len(source), "complete source coverage", len(source), (committed, parser.committed))
        require(node_index == len(expected), "complete AST coverage", len(expected), node_index)
        if reference is not None:
            require(units == reference, "partition-independent code-unit spans", reference, units)
    except Mismatch as error:
        return None, {"kind": "mismatch", **context, **error.detail}
    except Exception as error:  # noqa: BLE001 - report parser failures per file without aborting the remaining corpus.
        # A valid whole-source AST that cannot stream is a conformance failure,
        # but distinguish parser/compiler errors from explicit invariant failures.
        return None, {"kind": "parse_error", **context, "exception": type(error).__name__, "message": str(error)[:700], "lineno": getattr(error, "lineno", None)}
    return units, None


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(sysconfig.get_path("stdlib")), help="source tree; defaults to the host stdlib")
    parser.add_argument("--limit", type=int, default=500, help="maximum files considered; 0 scans the whole tree (default: 500)")
    parser.add_argument("--chunk-max", type=int, default=4096, help="largest deterministic random text chunk (default: 4096)")
    parser.add_argument("--seed", type=int, default=32171, help="seed combined with each relative path (default: 32171)")
    parser.add_argument("--char-limit", type=int, default=0, help="also feed one character at a time for files at most this many characters; 0 disables")
    parser.add_argument("--progress-every", type=int, default=25, help="emit progress after this many files (default: 25)")
    parser.add_argument("--max-details", type=int, default=20, help="maximum details per error category; aggregate counts remain complete")
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    if not args.root.is_dir():
        parser.error("--root must be an existing directory")
    if args.limit < 0 or args.char_limit < 0 or args.max_details < 0 or args.chunk_max < 1 or args.progress_every < 1:
        parser.error("limit, char-limit and max-details must be nonnegative; chunk-max and progress-every must be positive")
    return args


def main():
    args = arguments()
    started = perf_counter()
    totals = {"considered": 0, "valid": 0, "passed": 0, "failed_files": 0, "read_errors": 0, "whole_source_errors": 0, "parse_errors": 0, "mismatches": 0, "partitions": 0, "characters": 0}
    details = {}
    slowest = []

    def detail(category, **fields):
        count = details.get(category, 0)
        if count < args.max_details:
            emit(category, **fields)
        details[category] = count + 1

    def progress(event):
        emit(event, **totals, seconds=round(perf_counter() - started, 4))

    emit(
        "configuration",
        root=str(args.root),
        limit=args.limit,
        chunk_max=args.chunk_max,
        seed=args.seed,
        char_limit=args.char_limit,
        python=sys.version,
        source_text="tokenize.open decoded universal-newline text",
        execution="AST compilation only; corpus code is never executed",
    )
    interrupted = False
    try:
        for path in candidates(args.root):
            if args.limit and totals["considered"] >= args.limit:
                break
            totals["considered"] += 1
            relative = path.relative_to(args.root).as_posix()
            file_started = perf_counter()
            try:
                with tokenize.open(path) as handle:
                    source = handle.read()
            except (OSError, SyntaxError, UnicodeError, LookupError) as error:
                totals["read_errors"] += 1
                detail("read_error", path=relative, exception=type(error).__name__, message=str(error)[:700])
                if totals["considered"] % args.progress_every == 0:
                    progress("progress")
                continue
            try:
                expected = [signature(node) for node in tree(source, str(path)).body]
            except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
                totals["whole_source_errors"] += 1
                detail("whole_source_error", path=relative, exception=type(error).__name__, message=str(error)[:700], lineno=getattr(error, "lineno", None))
                if totals["considered"] % args.progress_every == 0:
                    progress("progress")
                continue
            totals["valid"] += 1
            totals["characters"] += len(source)
            file_seed = args.seed ^ int.from_bytes(hashlib.sha256(relative.encode("utf-8", "surrogatepass")).digest()[:8], "big")
            source_hash = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
            modes = ["bulk", "random"] + (["characters"] if args.char_limit and len(source) <= args.char_limit else [])
            reference = None
            failed = False
            for mode in modes:
                totals["partitions"] += 1
                units, failure = check_partition(source, path, expected, mode, args.chunk_max, file_seed, reference)
                if failure is not None:
                    failed = True
                    totals["parse_errors" if failure["kind"] == "parse_error" else "mismatches"] += 1
                    detail(failure["kind"], path=relative, source_sha256=source_hash, chunk_max=args.chunk_max, **failure)
                elif mode == "bulk":
                    reference = units
            totals["failed_files" if failed else "passed"] += 1
            slowest.append({"path": relative, "seconds": round(perf_counter() - file_started, 4), "characters": len(source), "modes": modes})
            slowest.sort(key=lambda item: item["seconds"], reverse=True)
            del slowest[5:]
            if totals["considered"] % args.progress_every == 0:
                progress("progress")
    except KeyboardInterrupt:
        interrupted = True
    emit(
        "summary",
        **totals,
        seconds=round(perf_counter() - started, 4),
        interrupted=interrupted,
        slowest=slowest,
        suppressed_details={key: max(0, value - args.max_details) for key, value in details.items()},
    )
    return 130 if interrupted else int(totals["failed_files"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
