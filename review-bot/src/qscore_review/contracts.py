"""Bot-owned envelope and review contracts (19 §§5,7; ChP-0049)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RepoConfig(StrictModel):
    version: Literal[1] = 1
    gemini: bool = False
    manifest: str = "qscore.yaml"
    target: str | None = None
    fail_on: Literal["error", "regression", "new_high"] | None = None
    general_comments: int = Field(default=3, ge=0, le=3)
    max_files: int = Field(default=30, ge=1, le=100)
    exclude: list[str] = Field(default_factory=lambda: ["data/**", "**/*.ipynb_checkpoints/**"])
    language: str = "en"


class BundleProvenance(StrictModel):
    repository: str
    repository_id: int = Field(gt=0)
    pull_request: int = Field(gt=0)
    workflow_id: int = Field(gt=0)
    workflow_path: str
    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    engine_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    score_version: str
    settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fork: bool


class BundleSide(StrictModel):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    exit_code: Literal[0, 1, 2, 3, 4]
    run: str | None = None
    scorecards: list[str] = Field(default_factory=list)
    compiled_qasm: list[str] = Field(default_factory=list)
    issues: str | None = None
    discovery: str | None = None
    error: str | None = None


class DetectedVersion(StrictModel):
    package: str
    value: str | None
    source: str | None
    line: int | None = Field(default=None, ge=1)


class ReviewBundle(StrictModel):
    schema_id: Literal["qscore.review-bundle/v0.1"] = Field(alias="schema")
    provenance: BundleProvenance
    config: RepoConfig
    base: BundleSide
    head: BundleSide
    detected_versions: list[DetectedVersion]
    checksums: dict[str, str]


Category = Literal[
    "circuit-regression",
    "diagnostic",
    "qiskit-api",
    "reproducibility",
    "measurement-and-intent",
    "manifest",
    "general",
]


class ReviewComment(StrictModel):
    path: str
    line: int = Field(ge=1)
    side: Literal["RIGHT"]
    category: Category
    severity: Literal["info", "warning", "error"]
    body: str = Field(min_length=1, max_length=6000)
    suggestion: str | None = None
    evidence: list[str] = Field(min_length=1)
    assumes_versions: dict[str, str]


class ReviewDraft(StrictModel):
    schema_id: Literal["qscore.review/v0.1"] = Field(alias="schema")
    summary_note: str = Field(max_length=600)
    comments: list[ReviewComment] = Field(max_length=100)
