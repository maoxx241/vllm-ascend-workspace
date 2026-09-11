# Observational comparability certificate

Status: current

Comparison tools use `.agents/lib/vaws_comparability.py` to separate intended
changes from confounding changes and missing evidence. They create and consume
the certificate internally. Agents provide business inputs and actual outputs;
they do not fill identity forms or certify their own results.

The certificate compares identity leaves from both runs, retaining each leaf's
origin. It is comparable only when required groups and observed prefixes are
present, declared configuration agrees with observations, unintended differences
are absent, and intended varying keys exist. Null and blank values remain unknown.
Consuming the certificate recomputes its verdict, so an edited verdict cannot
override missing evidence or conflicting observations.

| Evidence | Origin and scope |
|---|---|
| Source commits, environment profile, native build, machine and devices | Coordinator's immutable launch receipt after preflight |
| Offline engine arguments and model path | Arguments actually passed to the inference engine |
| Online engine arguments and model path | Server launch observation; client declarations alone remain declared |
| Serving arguments | Recorded managed launch command |
| Benchmark arguments, dataset, concurrency and request rate | Executed measurement; generated datasets include a content hash |
| Graph stage identity | Captured snapshot sidecar |
| Manifest configuration without a runtime observation | Declared, never promoted to observed |

The launch receipt is limited to the attested launch. It cannot prove that code,
weights or environment were not subsequently modified. Imported result files and
sidecars must come from the claimed execution; the comparator cannot authenticate
an invented observation. A model path alone does not prove weight-file contents.

| Entry | Inputs | Incomplete or incomparable evidence |
|---|---|---|
| `correctness_run.py` | Cases and baseline/candidate outputs | Terminal inconclusive report with raw outputs retained |
| `performance_regression.py` | Business experiment or existing measurements | Terminal inconclusive report; no regression verdict |
| `graph_debug_case.py` | Eager/graph snapshots and observed sidecars | Inconclusive snapshot comparison |
| `change_validation.py` | Diff and existing validation manifests | Missing coverage remains visible; scope and code identities must match |

Performance experiments that bind baseline and candidate source worktrees derive
their intended code and compiled-artifact variables from the recorded identities.
Workload, machine and environment changes remain confounders. Report-only inputs
can declare intended variables through `allowed_differences`; correctness and
graph comparisons expose `--allowed-difference` for the requested experiment.
Within-state measurement inconsistency is checked before comparing states.

Single-run distributed diagnosis and isolated operator reports make claims about
their supplied evidence. They do not imply a two-state regression experiment or
a successful whole-model rerun. Missing cases, absent artifacts and unrelated
kernel identities cannot establish completed validation.

The local fixture matrix covers identical runs, code-only changes, eager/graph
changes, TP/device changes, DP cases and concurrency differences. These are
control-plane tests; Ascend hardware experiments are separate execution evidence.
