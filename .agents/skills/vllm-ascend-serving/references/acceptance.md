# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.
