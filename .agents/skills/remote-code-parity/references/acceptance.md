# Acceptance

- `--execution-id` returns failed/blocked without mutating a live root.
- Missing `--host` fails closed with Result Envelope v1 on stdout.
- Direct sync is source-only. Consent/sync-mode/first-install are not
  required.
- `--runtime-root` is optional and defaults to `/vllm-workspace`.
- Progress is on stderr; stdout is one JSON envelope.
