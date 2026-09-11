# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

Use --diff-file for an already captured diff. The report classifies affected components, derives supported coverage from actual evidence and exact code identities, and lists missing checks. Agents do not enter coverage labels or lifecycle records.
