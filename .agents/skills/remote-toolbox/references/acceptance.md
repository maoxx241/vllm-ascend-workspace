# Remote Toolbox Acceptance

- `python3 -m compileall -q .agents`
- `git diff --check -- .agents AGENTS.md README.md README.en.md`
- Every new `remote_*` CLI accepts `--help`.
- `remote_probe` succeeds on 125 and 131 and reports CANN, Python, torch,
  torch_npu, NPU facts, and host/container endpoints.
- Two concurrent 131 sessions have different container names, SSH ports, NPU
  leases, and state namespaces.
- `remote_sync_plan --mode source-only` reports `will_install=false`.
- `remote_sync_plan --mode install` reports install/rebuild reasons and consent.
- Invalid model service start returns `needs_input`; cleanup leaves no session
  service state running.
- Artifact manifest and pull cover at least one service log or profiling/report
  directory.
- Final session list, lease state, and service state prove test sessions were
  cleaned up.
