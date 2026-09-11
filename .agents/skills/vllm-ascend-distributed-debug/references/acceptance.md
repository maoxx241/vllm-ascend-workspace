# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts. The report validates mappings and event order and generates its evidence automatically; no case initialization or event-registration steps are required.
