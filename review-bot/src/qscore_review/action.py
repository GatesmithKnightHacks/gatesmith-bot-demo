"""Unprivileged CI producer for qscore.review-bundle/v0.1 (19 §§2,7).

Repository Python is executed only by qscore in the repository's CI. The receiving
App treats these files as data and independently authenticates the provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast
from urllib.request import Request, urlopen

from qscore.schemas.manifest import ProjectManifest
from qscore.schemas.run import RunRecord
from qscore.schemas.versions import SCORE_VERSION
from ruamel.yaml import YAML

from qscore_review.contracts import BundleProvenance, BundleSide, RepoConfig, ReviewBundle
from qscore_review.versions import detect_versions

ENGINE_REVISION = "6b42f722730f19a5c3d9b72e1a19cf8d54a0fedd"
MAX_PAYLOAD_BYTES = 32_000_000
MAX_FILE_BYTES = 8_000_000
MAX_FILES = 2000
ExitCode = Literal[0, 1, 2, 3, 4]
Runner = Callable[[Path, Path, RepoConfig], ExitCode]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError(f"unsafe repository path: {relative!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"repository path escapes checkout: {relative!r}")
    return resolved


def load_config(base_root: Path) -> RepoConfig:
    """Opt-in and policy are controlled by the base commit, never PR text."""
    path = _safe_path(base_root, ".github/qscore-review.yml")
    if not path.exists():
        return RepoConfig()
    if path.stat().st_size > 64_000:
        raise ValueError("qscore-review config exceeds 64000 bytes")
    value = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    config = RepoConfig.model_validate(value)
    _safe_path(base_root, config.manifest)
    return config


def _clean_environment(out: Path) -> dict[str, str]:
    """CI credentials must not reach qscore or its repository builder subprocesses."""
    allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
    env = {name: value for name, value in os.environ.items() if name in allowed}
    env.update(
        QSCORE_PROFILE="ci",
        QSCORE_DEVICE_DEFAULT="cpu",
        QSCORE_FALLBACKS="",
        QSCORE_CACHE_DIR=str(out.parent.parent / "qscore-cache" / out.name),
        PYTHONNOUSERSITE="1",
    )
    return env


def _cli(root: Path, out: Path, config: RepoConfig) -> ExitCode:
    manifest = _safe_path(root, config.manifest)
    command = [
        str(Path(sys.executable).with_name("qscore")),
        "score",
        str(manifest),
        "--out",
        str(out),
        "--yes",
    ]
    if config.target is not None:
        command.extend(["--target", config.target])
    completed = subprocess.run(
        command,
        cwd=root,
        env=_clean_environment(out),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    (out / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (out / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode not in (0, 1, 2, 3, 4):
        raise RuntimeError(f"qscore terminated with unexpected exit {completed.returncode}")
    if completed.returncode != 0:
        _write_json(
            out / "error.json",
            {"exit_code": completed.returncode, "message": completed.stderr, "command": command},
        )
    if completed.returncode == 2:
        # Public scoring API validates before asking for confirmation. This is a
        # separate validation-only pass; its evidence never changes the CLI verdict.
        validation = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "qscore_review.action",
                "validate",
                "--root",
                str(root),
                "--out",
                str(out),
                "--manifest",
                config.manifest,
                *(["--target", config.target] if config.target is not None else []),
            ],
            cwd=root,
            env=_clean_environment(out),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        (out / "revalidation.txt").write_text(
            validation.stdout + validation.stderr, encoding="utf-8"
        )
        if validation.returncode != 0:
            _write_json(
                out / "revalidation-error.json",
                {
                    "message": (
                        "Validation-only issue capture failed; original CLI status is retained"
                    ),
                    "exit_code": validation.returncode,
                    "stderr": validation.stderr,
                },
            )
    return cast(ExitCode, completed.returncode)


def _validate(root: Path, out: Path, manifest: str, target: str | None) -> None:
    from qscore import services

    try:
        source = _safe_path(root, manifest)
        if source.suffix == ".qasm":
            services.score_circuit(source, out / "unused", target, confirm=lambda _: False)
        else:
            services.score_project(source, out / "unused", confirm=lambda _: False)
    except services.InvalidInput as exc:
        if exc.report is not None:
            _write_json(out / "issues.json", exc.report.model_dump(mode="json"))


def _discover(root: Path, out: Path) -> None:
    from qscore import services

    # Missing manifest is a declared discovery state, never a synthesized scoring
    # configuration. The reviewed draft preserves every <required> placeholder.
    result = services.discover_candidates(root)
    pick = ",".join(str(candidate.index) for candidate in result.candidates)
    draft = None
    if result.candidates:
        draft = services.discover_candidates(root, draft=True, pick=pick, no_input=True)
        if draft.draft_path is not None:
            (out / "qscore.suggested.yaml").write_text(
                draft.draft_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    _write_json(
        out / "discovery.json",
        {
            "candidates": [asdict(candidate) for candidate in result.candidates],
            "draft": "qscore.suggested.yaml" if draft is not None else None,
            "diff": draft.diff if draft is not None else "",
            "reason": (
                "No configured manifest; discovery only. Review/fill the draft before scoring."
            ),
        },
    )


def _side(root: Path, out: Path, commit: str, config: RepoConfig, runner: Runner) -> BundleSide:
    out.mkdir(parents=True)
    if not _safe_path(root, config.manifest).is_file():
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "qscore_review.action",
                "discover",
                "--root",
                str(root),
                "--out",
                str(out),
            ],
            cwd=root,
            env=_clean_environment(out),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        (out / "discovery-log.txt").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
        if completed.returncode != 0:
            _write_json(
                out / "error.json",
                {"exit_code": 3, "message": completed.stderr, "operation": "discovery"},
            )
            return BundleSide(commit=commit, exit_code=3, error=f"{out.name}/error.json")
        return BundleSide(commit=commit, exit_code=0, discovery=f"{out.name}/discovery.json")
    exit_code = runner(root, out, config)
    run_path = out / "run.json"
    scorecards: list[str] = []
    compiled: list[str] = []
    if run_path.is_file():
        record = RunRecord.model_validate_json(run_path.read_text(encoding="utf-8"))
        if record.provenance.score_version != SCORE_VERSION:
            raise ValueError("engine run score version differs from installed engine")
        scorecards = [f"{out.name}/{ref.path}" for ref in record.scorecards]
        compiled = [f"{out.name}/{ref.path}" for ref in record.compiled]
        for relative in [*scorecards, *compiled]:
            _safe_path(out.parent, relative)
    return BundleSide(
        commit=commit,
        exit_code=exit_code,
        run=f"{out.name}/run.json" if run_path.is_file() else None,
        scorecards=scorecards,
        compiled_qasm=compiled,
        issues=f"{out.name}/issues.json" if (out / "issues.json").is_file() else None,
        error=f"{out.name}/error.json" if (out / "error.json").is_file() else None,
    )


def _checksums(out: Path) -> dict[str, str]:
    paths = sorted(path for path in out.rglob("*") if path.is_file())
    if len(paths) > MAX_FILES:
        raise ValueError(f"bundle exceeds {MAX_FILES} payload files")
    total = 0
    checksums = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(out.resolve()):
            raise ValueError(f"bundle payload escapes output: {path}")
        if path.suffix not in {".json", ".qasm", ".yaml", ".txt", ".md"}:
            raise ValueError(f"unsupported bundle payload type: {path.suffix}")
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_PAYLOAD_BYTES:
            raise ValueError("bundle payload exceeds size limit")
        checksums[path.relative_to(out).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return checksums


def _evaluation_settings(
    root: Path, config: RepoConfig, side: BundleSide, runtime: dict[str, Any], out: Path
) -> dict[str, Any]:
    """Conservative declarations, not a duplicate implementation of engine resolution.

    Equal declarations under the same immutable engine preserve the same defaults.
    Equivalent changes to where defaults are declared can be incomparable; callers
    must describe that limitation instead of claiming these are resolved settings.
    """
    settings = {**runtime, "comparison": "conservative_declared_evaluation/v1", "items": {}}
    if side.run is None:
        return settings
    record = RunRecord.model_validate_json((out / side.run).read_text(encoding="utf-8"))
    source = _safe_path(root, config.manifest)
    if source.suffix == ".qasm":
        settings["items"] = {
            item: {"input_mode": "single-qasm", "target": config.target} for item in record.items
        }
        return settings
    manifest = ProjectManifest.model_validate(
        YAML(typ="safe").load(source.read_text(encoding="utf-8"))
    )
    project_root = (source.parent / manifest.project.root).resolve()
    if not project_root.is_relative_to(root.resolve()):
        raise ValueError("evaluation project.root escapes source checkout")
    defaults = manifest.defaults.model_dump(mode="json", exclude_none=True)
    for item in manifest.circuits:
        declared = item.model_dump(mode="json", exclude_none=True, exclude={"id", "source"})
        references: dict[str, Any] = {}
        for prefix, declaration in (("defaults", defaults), ("item", declared)):
            calibration = declaration.get("target", {}).get("calibration")
            observable = declaration.get("invariants", {}).get("task", {}).get("observable")
            files = {}
            if isinstance(calibration, dict) and isinstance(calibration.get("file"), str):
                files["target.calibration"] = calibration["file"]
            if isinstance(observable, str):
                files["invariants.task.observable"] = observable
            for field, reference in files.items():
                path = (project_root / reference).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise ValueError(f"evaluation reference escapes source checkout: {reference}")
                key = f"{prefix}.{field}:{reference}"
                if not path.is_file():
                    # An overridden declaration can refer to an unused missing file.
                    references[key] = {"missing": True}
                else:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError(f"evaluation reference exceeds size limit: {reference}")
                    references[key] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        settings["items"][item.id] = {
            "defaults": defaults,
            "declared": declared,
            "runner": manifest.project.runner.model_dump(mode="json"),
            "referenced_files": references,
        }
    return settings


def produce_bundle(
    base_root: Path,
    head_root: Path,
    out: Path,
    provenance: BundleProvenance,
    *,
    runner: Runner = _cli,
) -> ReviewBundle:
    """Score independently checked-out commits and write a checksummed data bundle."""
    if out.exists():
        raise ValueError("bundle output already exists; refusing to mix prior run artifacts")
    if out.resolve().is_relative_to(base_root.resolve()) or out.resolve().is_relative_to(
        head_root.resolve()
    ):
        raise ValueError("bundle output must be outside both source checkouts")
    config = load_config(base_root)
    settings = {
        "engine_revision": provenance.engine_revision,
        "profile": "ci",
        "device_default": "cpu",
        "fallbacks": [],
        "optimization_level": 3,
        "manifest": config.manifest,
        "target": config.target,
    }
    fingerprint = hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    out.mkdir(parents=True)
    base = _side(base_root, out / "base", provenance.base_sha, config, runner)
    head = _side(head_root, out / "head", provenance.head_sha, config, runner)
    _write_json(
        out / "base/settings.json", _evaluation_settings(base_root, config, base, settings, out)
    )
    _write_json(
        out / "head/settings.json", _evaluation_settings(head_root, config, head, settings, out)
    )
    bundle = ReviewBundle.model_validate(
        {
            "schema": "qscore.review-bundle/v0.1",
            "provenance": provenance.model_copy(update={"settings_sha256": fingerprint}),
            "config": config,
            "base": base,
            "head": head,
            "detected_versions": detect_versions(head_root),
            "checksums": _checksums(out),
        }
    )
    _write_json(out / "envelope.json", bundle.model_dump(mode="json", by_alias=True))
    return bundle


def _git_commit(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _metadata() -> dict[str, Any]:
    """Read own run metadata using an Actions read-only token, before isolation."""
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    request = Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("workflow run metadata exceeds size limit")
    return json.loads(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("bundle", "discover", "validate"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", default="qscore.yaml")
    parser.add_argument("--target")
    parser.add_argument("--engine-revision", default=ENGINE_REVISION)
    args = parser.parse_args()
    if args.operation in ("discover", "validate"):
        if args.root is None:
            parser.error("--root is required")
        if args.operation == "discover":
            _discover(args.root.resolve(), args.out.resolve())
        else:
            _validate(args.root.resolve(), args.out.resolve(), args.manifest, args.target)
        return
    if args.base is None or args.head is None:
        parser.error("--base and --head are required")
    if args.engine_revision != ENGINE_REVISION:
        parser.error("engine revision must match this Action release's immutable engine pin")
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        parser.error("this Action only accepts unprivileged pull_request events")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pr = event["pull_request"]
    base, head = args.base.resolve(), args.head.resolve()
    if _git_commit(base) != pr["base"]["sha"] or _git_commit(head) != pr["head"]["sha"]:
        raise ValueError("source checkout SHA does not match pull request event")
    metadata = _metadata()
    provenance = BundleProvenance(
        repository=event["repository"]["full_name"],
        repository_id=event["repository"]["id"],
        pull_request=pr["number"],
        workflow_id=metadata["workflow_id"],
        workflow_path=metadata["path"],
        run_id=int(os.environ["GITHUB_RUN_ID"]),
        run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
        engine_revision=ENGINE_REVISION,
        score_version=SCORE_VERSION,
        settings_sha256="0" * 64,
        fork=pr["head"]["repo"]["id"] != event["repository"]["id"],
    )
    produce_bundle(base, head, args.out.resolve(), provenance)


if __name__ == "__main__":
    main()
