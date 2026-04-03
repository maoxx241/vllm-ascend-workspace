# Runtime Bootstrap Triage

Use this file only after `runtime.bootstrap_container_transport` fails.

## Triage Order

1. Read the tool result first. Record `status`, `reason`, and whether a return code is present.
2. If the failure text contains `rc=143`, inspect process-kill patterns before assuming package or network failure.
3. Read `tools/atomic/runtime_bootstrap_container_transport.py`, then `tools/lib/runtime_transport.py`.
4. Read `tools/lib/runtime_container.py` only if the failing stage is still ambiguous after `runtime_transport.py`.

## Do Not Do

- Do not start with `.agents/discovery/README.md`.
- Do not fan out into sibling `tools/lib/*.py` modules before the failing stage is known.
- Do not treat SSH banner text or package-manager warnings as root cause without a return code.
