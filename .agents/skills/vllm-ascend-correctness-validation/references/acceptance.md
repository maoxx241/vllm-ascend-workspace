# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.
