---
name: modelscope
description: "Download, resume, status-check, and SHA256-verify ModelScope model weights. Use for $modelscope download/status/verify/check, Chinese requests to 下载/续传/补全/查看进度/校验 ModelScope 权重, and tasks that need durable background ModelScope downloads under explicit local directories."
---

# ModelScope

Use the bundled scripts from this skill directory. Prefer the compact manager first:

- `scripts/modelscope_auto.py` - status, auto-resume, background download, and post-download verification
- `scripts/download_from_modelscope.py` - low-level single-model downloader
- `scripts/modelscope_download_status.py` - low-level size status
- `scripts/verify_modelscope_sha256.py` - low-level SHA256 verification

Do not inline long `nohup`/`setsid` shell blocks. Do not read or tail large logs unless a task fails or the user asks.

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
python3 "$SKILL_DIR/scripts/modelscope_auto.py" ensure \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

`ensure` behavior:

- If a task is active, leave it running and report compact status.
- If official files are incomplete and no task is active, start a detached background worker in the same `LOCAL_DIR`.
- If files are complete but verification is missing or stale, start detached SHA256 verification.
- If verification reports real missing, size mismatch, or SHA256 mismatch, report it and ask before repair.
- It preserves partial files and never deletes weights.

The manager writes `download.pid`, `download.launch.log`, `download.log`, `verify.log`, `modelscope_sha256.report.json`, `modelscope_sha256.tsv`, and `SHA256SUMS` in `LOCAL_DIR`.

Proxy options:

- Pass no proxy option by default.
- Add `--no-proxy` only when requested.
- Add `--proxy "$PROXY_URL"` only when provided.

## Status

For explicit status only:

```bash
python3 "$SKILL_DIR/scripts/modelscope_auto.py" status \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

If the user asks for all tasks, require a root and run `--root ROOT`; the script discovers `*/download.pid` and infers `namespace/name` from the last two path components.

Report only the compact script output: state, percent, local/expected size, PID, verification state, and directory. Include log paths only when useful.

## Verify

For explicit verification:

```bash
python3 "$SKILL_DIR/scripts/modelscope_auto.py" verify \
  --model "$MODEL_ID=$LOCAL_DIR" \
  --revision "$REVISION"
```

Verification ignores `.gitattributes` by default because it is Git metadata, not model weight content. Do not repair or redownload solely because `.gitattributes` is absent.

## Output Rules

- Keep responses short.
- Do not paste large command output or progress bars.
- Summarize each model as `state`, percent, PID, verification result, and paths.
- If network or filesystem sandboxing blocks a required command, rerun with approval as needed.
