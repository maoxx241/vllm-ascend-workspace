# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report computes KEEP/DISCARD and verifies kernel lineage and case coverage.
