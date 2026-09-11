# Behavior

Diagnose failures whose behavior depends on ranks, nodes, process groups, collectives or distributed endpoints.

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts. The report validates mappings and event order and generates its evidence automatically; no case initialization or event-registration steps are required.

Start from the failing topology and per-rank timeline. Distinguish missing rank startup, rendezvous, collective ordering and asymmetric workloads. Reduce topology only when the reduced case still reproduces the signature.

Use graph-debug when eager passes and graph fails independent of topology. Performance imbalance with a successful run belongs to profiling-analysis.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
