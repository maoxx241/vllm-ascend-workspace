---
name: modelscope
description: "Download, resume, status-check, and SHA256-verify ModelScope model weights. Use for $modelscope download/status/verify/check, Chinese requests to 下载/续传/补全/查看进度/校验 ModelScope 权重, and tasks that need durable background ModelScope downloads under explicit local directories."
---

# ModelScope

Use the bundled manager with `uv run --no-project python` on each platform.
It starts downloads and verification in a background worker and reports compact status.

- `scripts/modelscope_auto.py` - status, auto-resume, background download, and post-download verification
- `scripts/download_from_modelscope.py` - low-level single-model downloader
- `scripts/modelscope_download_status.py` - low-level size status
- `scripts/verify_modelscope_sha256.py` - low-level SHA256 verification

Do not inline long `nohup`/`setsid` shell blocks. Do not read or tail large logs unless a task fails or the user asks.

Edit only `.agents/skills/modelscope`. `python3 .agents/scripts/sync_claude_skills.py` regenerates `.trae/skills/modelscope` from this package.

## Model Mapping

Represent every model as `MODEL_ID=LOCAL_DIR`.

- `MODEL_ID` must be `namespace/name`.
- `LOCAL_DIR` must be explicit.
- If the user says “to `/root`” without a model subdirectory, use `/root/namespace/name`.
- Use revision `master` unless specified.
- Repeat `--model MODEL_ID=LOCAL_DIR` for multiple models.

## Download / Resume / Auto Complete

For `$modelscope download`, resume, repair-after-approval, or “check and continue if incomplete”, run:

```bash
uv run --no-project python "$SKILL_DIR/scripts/modelscope_auto.py" ensure \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

`ensure` behavior:

- If a task is active, leave it running and report compact status.
- If official files are incomplete and no task is active, start a detached background worker in the same `LOCAL_DIR`.
- If files are complete but verification is missing or stale, start detached SHA256 verification.
- If verification reports a real mismatch, use the existing authorization to decide whether to repair; ask only if replacing those files was not authorized.
- It preserves partial files and never deletes weights.

The manager writes `download.pid`, `download.launch.log`, `download.log`, `verify.log`, `modelscope_sha256.report.json`, `modelscope_sha256.tsv`, and `SHA256SUMS` in `LOCAL_DIR`.

Pass `--auto-install` when the requested download needs a missing ModelScope SDK.
It resolves that dependency in an isolated uv environment and records the actual
SDK version and interpreter in the download log; it does not install into the
selected workspace environment. Use `--help` for optional concurrency settings.

Proxy options:

- Pass no proxy option by default.
- Add `--no-proxy` only when requested.
- Add `--proxy "$PROXY_URL"` only when provided.

## Status

For explicit status only:

```bash
uv run --no-project python "$SKILL_DIR/scripts/modelscope_auto.py" status \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

If the user asks for all tasks, require a root and run `--root ROOT`; the script discovers `*/download.pid` and infers `namespace/name` from the last two path components.

Report only the compact script output: state, percent, local/expected size, PID, verification state, and directory. Include log paths only when useful.

Completion requires every expected file to have the expected size. A SHA256
report is reused only while the model ID, revision, official sizes/hashes and
local file signatures still match. An old report or a changed file requires
verification again; status does not rehash large weight files.
The download record also retains the worker's birth time and command. A reused
PID alone does not count as an active download.

## Verify

For explicit verification:

```bash
uv run --no-project python "$SKILL_DIR/scripts/modelscope_auto.py" verify \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

Verification ignores `.gitattributes` by default because it is Git metadata, not model weight content. Do not repair or redownload solely because `.gitattributes` is absent.

## Output Rules

- Keep responses short.
- Do not paste large command output or progress bars.
- Summarize each model as `state`, percent, PID, verification result, and paths.
- If the execution policy rejects a required operation, report the exact rejection and do not bypass it.
