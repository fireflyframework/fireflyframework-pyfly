"""Extract python code listings from manuscript and verify they parse.

Used by build.py (warn-only) and runnable standalone:
    book/.venv/bin/python book/build/verify_code.py book/manuscript
"""
from __future__ import annotations
import ast, re, sys
from dataclasses import dataclass
from pathlib import Path

_START = re.compile(r'^:::\s*listing\s+(?P<file>[^|]+?)\s*(?:\|\s*(?P<cap>.+?))?\s*$')

@dataclass
class Listing:
    label: str
    code: str
    source: Path
    line: int

def extract_python_listings(md_file: Path) -> list[Listing]:
    out: list[Listing] = []
    lines = Path(md_file).read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        m = _START.match(lines[i])
        if not m:
            i += 1; continue
        label, start = m["file"].strip(), i + 1
        body, i = [], i + 1
        while i < len(lines) and lines[i].strip() != ":::":
            body.append(lines[i]); i += 1
        i += 1
        if label.endswith(".py"):
            out.append(Listing(label, "\n".join(body), Path(md_file), start))
    return out

def check_syntax(code: str) -> tuple[bool, str | None]:
    # allow elided bodies written as '...'; reject real syntax errors
    try:
        ast.parse(code)
        return True, None
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"

def main(root: str) -> int:
    files = sorted(Path(root).rglob("*.md"))
    failures = 0
    for f in files:
        for lst in extract_python_listings(f):
            ok, err = check_syntax(lst.code)
            if not ok:
                failures += 1
                print(f"FAIL {f}:{lst.line} [{lst.label}] {err}")
    print(f"verify_code: {failures} failing listing(s) across {len(files)} file(s)")
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "book/manuscript"))
