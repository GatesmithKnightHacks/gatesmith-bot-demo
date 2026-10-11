"""Static dependency evidence, preserving declared ranges (spec 19 §3.1).

No source file is imported or executed. Highest-priority declarations win; an
undeclared dependency has null value/source/line, rather than an inferred version.
"""

from __future__ import annotations

import ast
import configparser
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from qscore_review.contracts import DetectedVersion

PACKAGES = (
    "python",
    "qiskit",
    "qiskit-aer",
    "qiskit-ibm-runtime",
    "qiskit-algorithms",
    "qiskit-nature",
    "qiskit-optimization",
    "qiskit-machine-learning",
    "numpy",
)
MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", ".ipynb_checkpoints"}
_DEP = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*([^;]*)(?:;.*)?$")


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _wanted(name: str) -> bool:
    return name in PACKAGES or name.startswith("mqt-")


def _read(root: Path, path: Path) -> str:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"version declaration escapes repository: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"version declaration exceeds {MAX_FILE_BYTES} bytes: {path}")
    return path.read_text(encoding="utf-8")


def _line(text: str, needle: str) -> int:
    for index, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return index
    return 1  # document-level declaration (e.g. a compact JSON lock file)


def detect_versions(root: Path) -> list[DetectedVersion]:
    """Return declaration evidence in spec order, without resolving dependency ranges."""
    root = root.resolve()
    found: dict[str, DetectedVersion] = {}
    declarations: dict[str, list[dict[str, object]]] = {}

    def put(
        name: str,
        value: object,
        path: Path,
        text: str,
        needle: str | None = None,
        line: int | None = None,
    ) -> None:
        normalized = _name(name)
        if not _wanted(normalized) or not isinstance(value, str):
            return
        source = path.relative_to(root).as_posix()
        source_line = line if line is not None else _line(text, needle or name)
        existing = found.get(normalized)
        if existing is not None and existing.source != source:
            return  # preserve the source priority defined in §3.1
        declaration = {"value": value, "line": source_line}
        evidence = declarations.setdefault(normalized, [])
        if any(entry["value"] == value for entry in evidence):
            return
        evidence.append(declaration)
        result = (
            value
            if len(evidence) == 1
            else ("ambiguous declarations: " + json.dumps(evidence, sort_keys=True))
        )
        found[normalized] = DetectedVersion(
            package=normalized,
            value=result,
            source=source,
            line=existing.line if existing is not None else source_line,
        )

    def dependency(
        value: object, path: Path, text: str, *, pinned_only: bool = False, line: int | None = None
    ) -> None:
        if not isinstance(value, str):
            return
        match = _DEP.match(value)
        if match is None:
            return
        constraint = match[2].strip()
        if not constraint or (pinned_only and not constraint.startswith("==")):
            return
        if ";" in value:
            constraint = "conditional declaration: " + json.dumps(
                {"value": constraint, "markers": value.split(";", 1)[1].strip()}, sort_keys=True
            )
        put(match[1], constraint, path, text, value, line)

    for filename in ("uv.lock", "poetry.lock"):
        path = root / filename
        if not path.is_file():
            continue
        text = _read(root, path)
        document = tomllib.loads(text)
        put("python", document.get("requires-python"), path, text, "requires-python")
        packages: dict[str, list[dict[str, Any]]] = {}
        offsets: dict[str, int] = {}
        for package in document.get("package", []):
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                continue
            name = package["name"]
            locations = [
                index
                for index, entry in enumerate(text.splitlines(), 1)
                if re.match(r"^\s*name\s*=\s*['\"]" + re.escape(name) + r"['\"]", entry)
            ]
            offset = offsets.get(name, 0)
            package_line = locations[offset] if offset < len(locations) else 1
            offsets[name] = offset + 1
            declaration = {"version": package.get("version"), "line": package_line}
            for marker in ("resolution-markers", "markers", "python-versions"):
                if marker in package:
                    declaration[marker] = package[marker]
            packages.setdefault(name, []).append(declaration)
        for name, entries in packages.items():
            distinct = {entry["version"] for entry in entries if isinstance(entry["version"], str)}
            if len(distinct) > 1:
                version = "ambiguous lock declarations: " + json.dumps(entries, sort_keys=True)
            elif any(len(entry) > 2 for entry in entries):
                version = "conditional lock declaration: " + json.dumps(entries, sort_keys=True)
            else:
                version = entries[0]["version"]
            put(name, version, path, text, line=int(entries[0]["line"]))
    path = root / "Pipfile.lock"
    if path.is_file():
        text = _read(root, path)
        document = json.loads(text)
        requires = document.get("_meta", {}).get("requires", {})
        put(
            "python",
            requires.get("python_full_version", requires.get("python_version")),
            path,
            text,
            "python_",
        )
        for section in ("default", "develop"):
            for name, entry in document.get(section, {}).items():
                if isinstance(entry, dict):
                    put(name, entry.get("version"), path, text)
    requirements: list[tuple[Path, str]] = []
    for path in sorted(root.glob("requirements*.txt")):
        text = _read(root, path)
        requirements.append((path, text))
        for index, entry in enumerate(text.splitlines(), 1):
            dependency(entry.split(" #", 1)[0], path, text, pinned_only=True, line=index)

    path = root / "pyproject.toml"
    if path.is_file():
        text = _read(root, path)
        document = tomllib.loads(text)
        project = document.get("project", {})
        put("python", project.get("requires-python"), path, text, "requires-python")
        project_dependencies = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            project_dependencies.extend(group)
        for group in document.get("dependency-groups", {}).values():
            project_dependencies.extend(entry for entry in group if isinstance(entry, str))
        for entry in project_dependencies:
            dependency(entry, path, text)
        poetry = document.get("tool", {}).get("poetry", {})
        poetry_dependencies = dict(poetry.get("dependencies", {}))
        for group in poetry.get("group", {}).values():
            for name, entry in group.get("dependencies", {}).items():
                poetry_dependencies.setdefault(name, entry)
        for name, entry in poetry_dependencies.items():
            put(name, entry.get("version") if isinstance(entry, dict) else entry, path, text)
    for path, text in requirements:
        for index, entry in enumerate(text.splitlines(), 1):
            dependency(entry.split(" #", 1)[0], path, text, line=index)

    path = root / "setup.cfg"
    if path.is_file():
        text = _read(root, path)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(text)
        if config.has_section("options"):
            put(
                "python",
                config.get("options", "python_requires", fallback=None),
                path,
                text,
                "python_requires",
            )
            for entry in config.get("options", "install_requires", fallback="").splitlines():
                dependency(entry, path, text)
        if config.has_section("options.extras_require"):
            for _, extra_lines in config.items("options.extras_require"):
                for entry in extra_lines.splitlines():
                    dependency(entry, path, text)
    path = root / "setup.py"
    if path.is_file():
        text = _read(root, path)
        tree = ast.parse(text, filename=str(path))
        literals: dict[str, ast.expr] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    literals[target.id] = statement.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                (isinstance(func, ast.Name) and func.id == "setup")
                or (isinstance(func, ast.Attribute) and func.attr == "setup")
            ):
                continue
            for keyword in node.keywords:
                value_node = keyword.value
                if isinstance(value_node, ast.Name):
                    value_node = literals.get(value_node.id, value_node)
                try:
                    value = ast.literal_eval(value_node)
                except (ValueError, TypeError):
                    continue  # computed declarations are not statically declared versions
                if keyword.arg == "python_requires":
                    put("python", value, path, text, line=keyword.value.lineno)
                elif keyword.arg == "install_requires" and isinstance(value, (list, tuple)):
                    for index, entry in enumerate(value):
                        entry_line = (
                            value_node.elts[index].lineno
                            if isinstance(value_node, (ast.List, ast.Tuple))
                            else keyword.value.lineno
                        )
                        dependency(entry, path, text, line=entry_line)

    path = root / "environment.yml"
    if path.is_file():
        text = _read(root, path)
        document = YAML(typ="safe").load(text)
        if isinstance(document, dict):
            for entry in document.get("dependencies", []):
                if isinstance(entry, dict):
                    for pip_entry in entry.get("pip", []):
                        dependency(pip_entry, path, text)
                elif isinstance(entry, str):
                    match = re.match(r"(?:[^:]+::)?([A-Za-z0-9_.-]+)([=<>!].*)$", entry)
                    if match:
                        put(match[1], match[2], path, text, entry)

    scanned = 0
    for directory, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        scanned += len(filenames)
        if scanned > 5000:
            raise ValueError("version scan exceeds 5000 repository files")
        for filename in sorted(filenames):
            if not filename.endswith(".ipynb"):
                continue
            path = Path(directory) / filename
            text = _read(root, path)
            notebook = json.loads(text)
            metadata = notebook.get("metadata", {})
            language = metadata.get("language_info", {})
            kernelspec = metadata.get("kernelspec", {})
            if language.get("name") == "python" or kernelspec.get("language") == "python":
                put("python", language.get("version"), path, text, '"version"')
    names = [*PACKAGES, *sorted(name for name in found if name.startswith("mqt-"))]
    if not any(name.startswith("mqt-") for name in found):
        names.append("mqt.*")
    return [
        found.get(name, DetectedVersion(package=name, value=None, source=None, line=None))
        for name in names
    ]
