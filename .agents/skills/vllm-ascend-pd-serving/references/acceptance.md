# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The business config contains services, connector, proxy and smoke workload; group_id and startup_order are optional. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config for proxy health; smoke takes --config. Coordinator owns resource state and teardown.
