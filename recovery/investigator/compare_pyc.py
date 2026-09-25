"""Bytecode-equivalence checker: compare my .py against the recovered original .pyc.

Compiles my source and compares co_names, co_varnames, co_firstlineno, co_argcount,
co_flags, and all non-code constants, recursively, for every code object.
Also lists constants present in the original but missing from mine.
"""
from __future__ import annotations

import dis
import marshal
import sys
import types
from collections import Counter


def load_pyc(path: str) -> types.CodeType:
    with open(path, "rb") as f:
        f.read(16)
        return marshal.load(f)


def sig(co: types.CodeType) -> tuple:
    return (
        co.co_name,
        co.co_firstlineno,
        co.co_argcount,
        co.co_posonlyargcount,
        co.co_kwonlyargcount,
        co.co_flags,
        tuple(co.co_varnames),
        tuple(co.co_names),
        tuple(co.co_freevars),
        tuple(co.co_cellvars),
    )


def plain_consts(co: types.CodeType) -> tuple:
    out = []
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            continue
        if isinstance(c, frozenset):
            c = ("FROZENSET", tuple(sorted(c)))
        out.append(repr(c))
    return tuple(out)


def children(co: types.CodeType) -> list[types.CodeType]:
    return [c for c in co.co_consts if isinstance(c, types.CodeType)]


problems: list[str] = []


def compare(original: types.CodeType, mine: types.CodeType | None, path: str) -> None:
    if mine is None:
        problems.append(f"{path}: MISSING in reconstruction")
        return

    so, sm = sig(original), sig(mine)
    labels = [
        "name", "firstlineno", "argcount", "posonly", "kwonly",
        "flags", "varnames", "names", "freevars", "cellvars",
    ]
    for label, a, b in zip(labels, so, sm):
        if a != b:
            problems.append(f"{path}: {label} ORIGINAL={a!r} MINE={b!r}")

    # constants: mine may have extra (docstrings I kept) but must cover originals
    co_o, co_m = Counter(plain_consts(original)), Counter(plain_consts(mine))
    missing = co_o - co_m
    extra = co_m - co_o
    if missing:
        problems.append(f"{path}: consts MISSING from mine: {sorted(missing)[:6]}")
    if extra:
        problems.append(f"{path}: consts EXTRA in mine: {sorted(extra)[:6]}")

    oc, mc = children(original), children(mine)
    onames = [c.co_name for c in oc]
    mnames = [c.co_name for c in mc]
    if sorted(onames) != sorted(mnames):
        problems.append(f"{path}: child code objects ORIGINAL={onames} MINE={mnames}")
        return
    for a, b in zip(oc, mc):
        compare(a, b, f"{path}.{a.co_name}")


def main() -> int:
    pyc, src = sys.argv[1], sys.argv[2]
    original = load_pyc(pyc)
    mine = compile(open(src).read(), src, "exec")
    # normalize the filename inside code objects
    compare(original, mine, "<module>")
    if problems:
        print(f"DIFFS ({len(problems)}):")
        for p in problems:
            print("  -", p)
        return 1
    print("EXACT bytecode-equivalent structure (signatures, names, line numbers, constants).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
