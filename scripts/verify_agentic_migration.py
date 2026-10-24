"""Check the frozen Agentic migration inventory without importing optional AI SDKs.

Run from any directory. Pass --source-root for independent source-commit/hash
verification; the default checks only the durable manifest and native checkout.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/agentic-migration-manifest.json"
LEGACY_NAMESPACE = "fireflyframework_agentic"


def module_path(root: Path, name: str) -> Path | None:
    path = root / "src" / name.replace(".", "/")
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def bindings(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Read module-scope names, declarations and literal lazy export tables."""
    names: set[str] = set()
    declarations: set[str] = set()
    exports: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
                declarations.add(node.name)
            elif isinstance(node, ast.TypeAlias):
                names.add(node.name.id)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                    exports.update(ast.literal_eval(node.value))  # type: ignore[arg-type]
                if any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in targets):
                    names.update(ast.literal_eval(node.value))  # type: ignore[arg-type]
            elif isinstance(node, ast.If | ast.Try | ast.TryStar | ast.With):
                visit(node.body)
                visit(getattr(node, "orelse", []))
                visit(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)

    visit(tree.body)
    return names, declarations, exports


def verify(root: Path, manifest: dict[str, Any], source_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if summary := manifest.get("summary"):
        if len(manifest["files"]) != summary["tracked_source_files"]:
            errors.append("Manifest source-file count differs from the frozen inventory")
        if len(manifest["modules"]) != summary["python_source_files"]:
            errors.append("Manifest source-module count differs from the frozen inventory")
    if source_root is not None:
        inventory = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", manifest["source"]["commit"]],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=True,
        )
        if set(inventory.stdout.splitlines()) != {item["source"] for item in manifest["files"]}:
            errors.append("Manifest source paths differ from the frozen Git tree")
        original_project = subprocess.run(
            ["git", "show", f"{manifest['source']['commit']}:pyproject.toml"],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=True,
        )
        original = tomllib.loads(original_project.stdout)["project"]
        if manifest.get("source_dependencies") != original["dependencies"]:
            errors.append("Original core dependencies differ from the frozen source")
        if manifest.get("source_optional_dependencies") != original["optional-dependencies"]:
            errors.append("Original optional dependencies differ from the frozen source")
    for item in manifest["files"]:
        target = item.get("target")
        if target and not (root / target).is_file():
            errors.append(f"Missing target: {target} (source {item['source']})")
        if source_root is not None:
            result = subprocess.run(
                ["git", "show", f"{manifest['source']['commit']}:{item['source']}"],
                cwd=source_root,
                capture_output=True,
                check=False,
            )
            if result.returncode or hashlib.sha256(result.stdout).hexdigest() != item["source_sha256"]:
                errors.append(f"Frozen source hash mismatch: {item['source']}")

    trees: dict[Path, ast.Module] = {}
    symbols: dict[Path, set[str]] = {}
    for path in sorted((root / "src/pyfly/agentic").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 12))
            trees[path] = tree
            symbols[path] = bindings(tree)[0]
        except (SyntaxError, ValueError) as exc:
            errors.append(f"Invalid Python/API syntax: {path.relative_to(root)}: {exc}")

    for item in manifest["modules"]:
        path = root / item["target"]
        if path not in trees:
            continue
        names, declarations, exports = bindings(trees[path])
        for category, expected, actual in (
            ("declarations", item["declarations"], declarations),
            ("exports", item["exports"], exports),
        ):
            if missing := set(expected) - actual:
                errors.append(f"Missing {category}: {item['target']}: {', '.join(sorted(missing))}")
        for name in exports - names:
            errors.append(f"Unbound export: {item['target']}: {name}")

    for path, tree in trees.items():
        label = path.relative_to(root)
        for name in symbols[path] & manifest["renames"].keys():
            errors.append(f"Legacy runtime binding: {label}: {name}")
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Name) and node.id in manifest["renames"]:
                errors.append(f"Legacy runtime symbol: {label}:{node.lineno}: {node.id}")
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name in manifest["renames"]
            ):
                errors.append(f"Legacy runtime declaration: {label}:{node.lineno}: {node.name}")
            imports: list[str] = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module]
                if node.module.startswith("pyfly.agentic"):
                    imported_path = module_path(root, node.module)
                    if imported_path is not None:
                        for alias in node.names:
                            if alias.name == "*":
                                continue
                            if (
                                alias.name not in symbols.get(imported_path, set())
                                and module_path(root, node.module + "." + alias.name) is None
                            ):
                                errors.append(f"Unbound native import: {label}:{node.lineno}: {alias.name}")
            for name in imports:
                if name == LEGACY_NAMESPACE or name.startswith(LEGACY_NAMESPACE + "."):
                    errors.append(f"Legacy namespace import: {label}:{line}: {name}")
                elif name.startswith("pyfly.agentic") and module_path(root, name) is None:
                    errors.append(f"Missing native import module: {label}:{line}: {name}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (node.value == LEGACY_NAMESPACE or node.value.startswith(LEGACY_NAMESPACE + "."))
            ):
                errors.append(f"Legacy namespace reference: {label}:{node.lineno}: {node.value}")

    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for name, dependencies in (manifest.get("supporting_extras", {}) | manifest["extras"]).items():
        if config.get("optional-dependencies", {}).get(name) != dependencies:
            errors.append(f"Extra dependencies changed: {name}; update the reviewed manifest explicitly")
    for name in manifest.get("source_optional_dependencies", {}):
        translation = manifest.get("dependency_translations", {}).get(name)
        if not translation or not translation.get("treatment"):
            errors.append(f"Missing dependency translation: {name}")
        elif name != "dev" and translation.get("native_extra") not in manifest["extras"]:
            errors.append(f"Missing translated native extra: {name}")
    for dependency in config.get("dependencies", []):
        if any(name in dependency.lower() for name in ("pydantic-ai", "pydantic-graph", "fireflyframework-agentic")):
            errors.append(f"AI dependency leaked into base install: {dependency}")
    entry = config.get("entry-points", {}).get("pyfly.auto_configuration", {}).get("agentic")
    if entry != "pyfly.agentic.auto_configuration:AgenticAutoConfiguration":
        errors.append("Missing native Agentic auto-configuration entry point")
    if (root / "src" / LEGACY_NAMESPACE).exists():
        errors.append("Legacy compatibility package exists")
    return sorted(set(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--json", type=Path, help="Write the verification result as JSON")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors = verify(args.root.resolve(), manifest, args.source_root)
    report = {
        "source_commit": manifest["source"]["commit"],
        "source_files": len(manifest["files"]),
        "source_modules": len(manifest["modules"]),
        "source_hashes_checked": args.source_root is not None,
        "errors": errors,
        "passed": not errors,
    }
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
