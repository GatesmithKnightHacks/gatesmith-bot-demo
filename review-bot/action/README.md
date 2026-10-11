# Gatesmith scoring Action

Copy [`examples/qscore-review.yml`](../examples/qscore-review.yml) into the
research repository's `.github/workflows/` directory. Replace
`REVIEW_BOT_COMMIT_SHA` with the full reviewed bot release commit. Register that
workflow's path and numeric ID in the App's allowlist.

Use `pull_request` and an Ubuntu runner. Grant the workflow token only
`contents: read` and `actions: read`; the latter reads the current workflow run's
ID/path. App and model credentials belong on the receiving server. Do not add
App, Gemini or Hugging Face secrets to this workflow.

The Action independently checks out the event's exact base/head commits without
persisting checkout credentials. The policy and Gemini opt-in come from the base
commit's `.github/qscore-review.yml`. Each side uses its own manifest at that same
configured path. The receiving App rejects comparisons whose target, seeds,
calibration or evaluation settings differ. Added/removed circuit IDs remain
visible. The Action also records a conservative per-item comparison of declared
defaults, objectives, invariants, execution settings, diagnostic policy and
referenced calibration/observable file hashes under the identical engine pin.
Circuit source content is excluded. Semantically equivalent edits to the way
defaults are declared can still be reported as incomparable; these declaration
records are not represented as resolved engine settings. PR manifests are never silently replaced with the base manifest.

The scorer is pinned to engine commit
`6b42f722730f19a5c3d9b72e1a19cf8d54a0fedd`. Changing that input requires a matching
reviewed Action release. qscore records its resolved dependency versions and
settings in `run.json` and scorecards. Repository dependencies are not installed
by this Action; missing imports are reported by the engine.

The artifact `qscore-review-bundle` contains `envelope.json`, `base/` and `head/`
run records, scorecards, compiled QASM and layout metadata, settings, logs and
payload checksums. Engine exit statuses are recorded in the envelope; engine
failure does not prevent the artifact from being uploaded. A producer failure
before the envelope is written fails the workflow instead.

For exit 2, a separate validation-only call on the same immutable checkout
captures machine-readable issues when available. Its log labels that pass and
never changes the original CLI result. No manifest means static discovery and a
suggested manifest with required placeholders; nothing is scored until the
researcher reviews and fills it. The bot never commits that draft.

Builder execution stays on the repository's CI runner. This Action strips CI and
provider credentials from the scoring environment, but qscore's builder process
is not a security boundary against arbitrary repository code. Use no sensitive
runner resources, secrets or privileged runner permissions in the job.
