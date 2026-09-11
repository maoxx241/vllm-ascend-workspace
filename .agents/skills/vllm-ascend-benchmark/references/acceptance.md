# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.
