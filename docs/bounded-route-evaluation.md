# Bounded three-route evaluator

`python -m app.bounded_route_evaluation` is a separate runner. It processes the
`rules`, `hybrid`, then `model` route in that order, and parallelizes notices
inside one route. Each notice runs in an isolated process, so the legacy
evaluator's temporary ingestion/model patches are never shared by worker
threads. `--concurrency` is the number of evaluator processes and defaults to
`1`; at `1`, notices retain the legacy input order and serial extraction.

The evaluator uses the deployment runtime directory and filesystem leases from
`app.bounded_runtime`. `DOCUMENT_WORKERS`, `MODEL_CONCURRENCY`, and
`JOB_QUEUE_CAPACITY` are independent deployment-wide limits. All web/job owners
and evaluator processes must point at the same local runtime directory. The
default is next to the configured database. Runtime leases are host-local and
must not use NFS. A second process with conflicting limits fails closed.

## Start and resume

Run from `backend` with a fresh output directory for each run:

```powershell
python -m app.bounded_route_evaluation `
  --gold .data/evaluation/gold.reviewed.json `
  --manifest .data/evaluation/manifest.csv `
  --source-root .data/evaluation/sources `
  --output-dir .data/evaluation/runs/bounded-20261009 `
  --scope html --pilot-only --concurrency 2
```

`--concurrency` rejects zero, negatives, and non-integers. Omit it to stay
serial. To resume, repeat the same command with the same output directory and
the same Gold, sources, model endpoint/configuration, extraction limits, code,
and concurrency. The runner checks this frozen identity before continuing;
finished notices are skipped. Interrupted/running notices are recovered as a
new attempt. Failed or incomplete notices remain visible and are not retried
unless `--retry-failed` is explicitly supplied. Attempts and their model-call
telemetry remain in `progress.json`.

`--max-calls-per-notice` is a budget for one notice in one route attempt. An
explicit retry starts a new attempt with a fresh budget; the option is not a
cumulative lifetime spend cap across retries. Review `progress.json` and the
provider's billing/usage controls when retrying failed notices.

To pause from another shell, run `python -m app.bounded_route_evaluation stop
--output-dir <run-directory>`; Ctrl+C does the same. The coordinator stops
submitting work, cancels work that has not started, lets active requests finish
or time out, and persists each completed result. A running request that loses
its connection may already have been billed remotely; retrying cannot guarantee
that the provider did not process it. After resolving provider quota/rate limits,
rerun the same command to resume. Add `--retry-failed` to retry failed/incomplete
notices. No automatic model/parameter fallback or unbounded 429 retry is
performed.

## Outputs and final-report compatibility

Only the coordinator writes `progress.json`. Each result has a stable task ID,
mode, notice ID, attempt, status, errors, model calls, and API usage. The
coordinator also writes the normal `predictions-{mode}.json`,
`evaluation-{mode}.json`, and summary files when routes complete. Route reports
use the same `completed[mode][notice_id]` layout and retain a nonempty
`identity.code_sha256`, so `route_evaluation_report` can validate completed
runs. For example, make a prep report for a completed pilot run with:

```powershell
python -m app.route_evaluation_report `
  --run-dir .data/evaluation/runs/bounded-20261009 `
  --gold .data/evaluation/gold.reviewed.json `
  --manifest .data/evaluation/manifest.csv `
  --output-json .data/evaluation/reports/bounded-prep.json `
  --output-markdown .data/evaluation/reports/bounded-prep.md `
  --stage prep --dataset-role tuning
```

The runner does not establish a frozen independent holdout and does not make a
pilot result eligible for `stage=final`. Use the existing report gate's normal
independence, dataset-role, and code-freeze requirements before producing a
final report.

The progress/config identity contains hashes for endpoints and keys, never raw
API keys. Each result has `mode`, `notice_id`, `attempt`, `status`, `errors`,
`model_calls`, and `api_usage`. Summary totals include actual fresh requests,
failed requests, elapsed time, provider-reported tokens, provider KV-cache tokens,
and local-cache restored history; cached requests do not count as new requests
in this run. Cache reads and writes use per-entry locks and atomic replacement.
This implementation supports the repository's existing
OpenAI-compatible adapter (including its Qwen/DeepSeek request differences); it
does not implement Anthropic or Gemini native protocols.
