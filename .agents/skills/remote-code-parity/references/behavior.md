# Remote-code-parity behavior

- Managed sources are prepared by coordinator. This wrapper never
  synchronizes or rebuilds into a live `--execution-id` root.
- Direct inspection requires `--host`. `--runtime-root` defaults to
  `/vllm-workspace` and is not a first-install gate.
- Apply mode is source-only. Materialize and install belong to coordinator
  preparation.
- There is no workspace consent/sync-mode ledger and no install-file
  classification in this skill.
- Local `.venv` package-import bootstrap may remain. Remote interpreter or
  CANN selection is coordinator-owned.
