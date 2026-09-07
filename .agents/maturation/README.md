# Deterministic-core maturation harness

Runs the declared deterministic remote operations (`operations.yaml`)
repeatedly, across several endpoints, in adversarial shapes, and reports pass
**rates** with per-failure layer attribution. It exercises the existing
`.remote-dev` tooling and never changes its behaviour.

What "mature" means, the thresholds per operation class, and the first-pass
results live in `docs/deterministic-core-maturation.md`.

## Usage

```bash
# list the declared operations
python3 .agents/maturation/run.py --list

# every host in the shared local inventory, default repetitions
python3 .agents/maturation/run.py --from-inventory

# hosts plus their managed containers, four endpoints at a time
python3 .agents/maturation/run.py --from-inventory --include-containers --endpoint-parallelism 4

# smoke: one explicit endpoint, two operations, two repetitions
python3 .agents/maturation/run.py --endpoint HOST:22 --operation bash.echo --operation read.seed --repetitions 2

# rebuild a report from retained evidence and capture reproducible failures
python3 .agents/maturation/run.py --report <run-id> --capture-knowledge
```

Progress is streamed on `stderr` as `__VAWS_MATURATION_PROGRESS__=<json>`;
`stdout` carries one JSON report. `kill -USR1 <pid>` dumps every thread's
stack when a run looks stalled.

## Safety properties

- Zero NPU devices, no model weights, no services. Only SSH, file, search,
  patch, job-registry, artifact and probe mechanics are exercised.
- Every remote write lands under `/tmp/vaws-maturation/<run-id>/<endpoint-label>/`
  and is removed at the end (`--keep-remote-scratch` to keep it). The path is
  per endpoint because a container may bind-mount the host's `/tmp`.
- An endpoint whose probe or scratch setup fails, or whose free disk / load
  exceeds the configured limits, is **skipped and reported**, never repaired.
- Host identities appear only in untracked evidence
  (`.vaws-local/maturation/runs/<run-id>/hosts.json`, `run.json`). The stdout
  report, `summary.md` and every knowledge candidate use `host-a` style
  labels; `--reveal-hosts` is an explicit opt-in.

## Evidence layout

```
.vaws-local/maturation/runs/<run-id>/
  hosts.json          label -> endpoint identity (only place identities live)
  trials.jsonl        one record per trial: command, environment, timing, raw output, attribution
  failures/*.json     full raw payloads for failed trials
  candidates/*.json   redacted knowledge candidates prepared from reproducible failures
  run.json            full report (identities included)
  summary.md          anonymised summary, safe to paste into a PR
```

## Knowledge feedback

A failure that reproduces at least `--min-reproductions` times (default 2)
with the same (operation, layer, fingerprint) becomes a redacted
`known-failure-signatures` candidate. With `--capture-knowledge` the harness
hands it to `.agents/scripts/knowledge_capture.py` as a CLI contract and
reports the script's verdict; a non-JSON or status-less reply is reported as
`contract-drift`, never patched around.

## Layout

| file | role |
|---|---|
| `operations.yaml` | declared operations, classes, thresholds (data) |
| `spec.py` | document contract and `{template}` rendering |
| `scenarios.py` | adversarial shapes and trial records |
| `attribution.py` | failure → layer attribution |
| `stats.py` | rates, Wilson bounds, flake/failure verdicts, ranking |
| `invoke.py` | in-process MCP dispatcher and killable CLI invocations |
| `targets.py` | inventory → endpoints → anonymous labels |
| `runner.py` | endpoint lifecycle, evidence, report, replay |
| `knowledge.py` | candidate builder and capture-CLI adapter |
| `redact.py` | identity scrubbing |
| `evidence.py` | untracked evidence store |
| `run.py` | CLI entrypoint |

Tests: `.agents/tests/test_maturation_harness.py` (no hardware needed).
