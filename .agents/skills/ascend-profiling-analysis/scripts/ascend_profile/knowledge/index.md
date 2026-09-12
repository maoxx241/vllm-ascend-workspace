# Analyzer data and references

This directory keeps analyzer implementation data and optional explanations
in their existing locations. The runtime data is versioned with the code;
it is not a format for workspace knowledge or a reading checklist for agents.
Workspace knowledge under `.agents/knowledge/` supplies related notes only.
Current measurements, model configuration and source evidence take precedence.

## Runtime data and test fixtures

Read these when changing the corresponding analyzer behavior. Their schemas
serve code and tests; ordinary analysis does not require editing them.

| File | Consumer and purpose |
|------|----------------------|
| `kernel_signatures.yaml` | `rules.py`: ordered kernel classification rules and source inventory |
| `attention_families.yaml` | `rules.py`: attention-family resolver and explanatory examples |
| `segmentation_rules.yaml` | `segment.py`: layer anchors and companion kernels |
| `diagnosis_rules.yaml` | `diagnostics.py`: thresholds and finding metadata; trigger logic remains in Python |
| `db_source_mapping.yaml` | `sources_db.py`: profiler database table and column mapping |
| `model_fingerprints.json` | Model context, segmentation and model insights: candidate fingerprints backed by configuration or profile evidence |
| `hardware_peak_measurements.json` | `hardware_insights.py`: measured sustained factors with capture conditions |
| `hardware_theoretical_peaks_cann9_0_0.json` | `hardware_insights.py`: versioned fallback when platform configuration is unavailable |
| `semantic_conventions.yaml` | Tests: expected output enums, not a runtime input or knowledge schema |

Runtime changes should update the affected code, data and regression tests
together. Model-family hints do not establish exact layer counts without
matching configuration or validated profile evidence. No prose note needs
promotion, registration or a special metadata format to be useful.

## Optional explanations

| Topic | Reference |
|-------|-----------|
| Pipeline fields and Cube/Vector interpretation | [pipeline_taxonomy.md](pipeline_taxonomy.md), [bound_classification.md](bound_classification.md) |
| Step boundaries, idle windows and repeated shapes | [step_anatomy.md](step_anatomy.md), [step_class_grouping.md](step_class_grouping.md) |
| Attention, FFN and MoE blocks | [block_taxonomy.md](block_taxonomy.md), [moe_families.yaml](moe_families.yaml) |
| HCCL capture levels and communication primitives | [communication_taxonomy.md](communication_taxonomy.md) |
| Architecture candidates and unresolved observations | [model_architectures.yaml](model_architectures.yaml), [model_knowledge_todo.md](model_knowledge_todo.md) |
| Earlier segmentation and classification failures | [known_counterexamples.md](known_counterexamples.md) |

`moe_families.yaml` and `model_architectures.yaml` are reference tables with
no production consumer. Their YAML form is historical, not a requirement for
new notes. Read only the material relevant to the question; discrepancies and
missing references do not block independent analysis.

## Reusing an existing analysis after a code or data change

| Changed behavior | Re-run from |
|------------------|-------------|
| Kernel classification | `--from-stage normalize` |
| Segmentation | `--from-stage segment` |
| Block or attention-family classification | `--from-stage classify` |
| Summary metrics | `--from-stage summarize` |
| Diagnosis | `--from-stage diagnostics` |
| Report rendering | `--from-stage report` |

Use `--remote-output-dir <abs-path>` to reuse the unaffected earlier stages.
Editing reference prose alone does not require rerunning a profile.
