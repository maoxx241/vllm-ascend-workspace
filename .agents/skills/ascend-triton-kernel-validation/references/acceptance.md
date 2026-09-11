# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The config contains op_name, reference, target, cases and tolerances. The tool checks kernel source for missing launches and computation fallback, combines observed case results, and emits coverage, analysis and a manifest automatically.
