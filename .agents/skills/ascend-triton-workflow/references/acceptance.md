# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The config contains op_name, source, target, cases and required_stages. One report call verifies stage scope, actual artifacts, passing cases and kernel identity. Missing or unrelated evidence cannot complete the workflow. Stage identifiers and linking are internal.
