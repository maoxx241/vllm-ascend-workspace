# Ascend memory attribution methodology

Use the selected coordinator execution for collection and ordinary remote-dev endpoint I/O for its files. No second allocation is needed to inspect or stop an existing execution.

## Collection

Standalone collection passes an msprof wrapper as a local input to the serving skill. The wrapper is embedded in the same managed run; its output is the runtime directory returned by that run. Requested model, parallelism, eager/graph and other serving options must match the report.

After HTTP readiness, capture npu-smi, startup logs and the inference response. Capture npu-smi again after the request. Stop the owned execution gracefully, then synchronously export its PROF directory and retain export failures. Do not infer completion by polling all msprof processes on a shared machine.

Attach collection uses the matching serving business receipt and execution ID. It leaves a live service running, so export may remain pending. A second collection into the same run requires that exact execution. There is no idle baseline unless an applicable measured baseline was explicitly supplied.

## Evidence boundaries

- npu-smi is a device snapshot. A measurement after inference is not an activation peak; its delta is reported separately from the earlier ready-state breakdown.
- msprof component maxima can come from different timestamps. Their sum is a diagnostic comparison, not an exact simultaneous allocation balance.
- Assign a CSV to a device only when its provenance identifies that device. Unassigned or multi-device process data stays in the global evidence section.
- Parse units from the actual CSV header and vLLM log. Normalize GB and GiB explicitly before comparison.
- Safetensors byte ranges describe stored tensor bytes exactly. Tensor-name categories, per-device sharding, GQA replication, quantization transforms and tied weights need model-specific interpretation. Label per-device weight values as estimates unless measured.
- Missing idle HBM, missing component CSVs and missing device samples remain unknown. Do not fill them with typical hardware values, inferred card indices or a fixed percentage.
- Expert parallelism comes from the execution configuration; an MoE model alone does not prove EP is enabled.
- Keep signed residuals visible. Do not force a residual below a fixed threshold or invent component labels to balance it.

Compare measurements from matching devices, ranks and time intervals. Where the available data cannot support a component-level statement, report the limitation and retain the raw artifact for further analysis.
