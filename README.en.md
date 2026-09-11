# vllm-ascend-workspace

**[中文](README.md)** | **English**

An Agent-only workspace for developing [vLLM](https://github.com/vllm-project/vllm) and [vLLM Ascend](https://github.com/vllm-project/vllm-ascend). People express goals and make substantive choices; the Agent edits code, prepares environments, runs experiments and reports evidence.

## Start with a task

Open this checkout in an Agent client and ask:

> Initialize this workspace for vLLM Ascend development.

The `repo-init` skill reuses existing configuration, installs locked packages and configures the selected client. Windows PowerShell and WSL can share the checkout: bootstrap selects separate platform environments automatically. Agent installation recipes are in [dependency-plane.md](docs/dependency-plane.md) and [windows-installation.md](docs/windows-installation.md).

For daily work, describe the outcome and the inputs that matter:

- Start a four-card inference service with these model weights and engine options.
- Compare throughput between these baseline and candidate worktrees.
- Collect a profile for this workload and identify the slow operators.
- Locate the first stage where graph and eager outputs diverge.
- Start the local fleet dashboard.

The Agent selects the relevant tool or skill. Tools generate execution references, state transitions and reports from actual results. Missing evidence remains unknown or inconclusive.

## Ownership and design

[Core design principles](docs/target-state.md#11-agent-only-design-principles) govern subsequent changes:

- Agent consumption is the design target for every code and command entry.
- Deterministic failures belong in component code and regression tests; contextual lessons belong in knowledge with their conditions and uncertainty.
- Each runtime owner handles its own lifecycle, validation and records. Business calls accept business inputs and evidence.
- Simplification is measured across the whole task. Unreleased APIs may change directly; retired interfaces have no compatibility aliases.

The workspace owns project materials, client wiring and business skills. `remote-dev` owns explicit endpoint I/O; `vaws-coordinator` owns managed sources, environments, NPUs and execution; `vaws-knowledge` owns Markdown lookup and capture; `vaws-top` owns fleet observation. Observation does not allocate devices. Native attachments establish task identity. Existing containers and unrelated worktrees are preserved.

## Business skills

| Skill                  | Purpose                                                                                      | When to use                                                |
| ---------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **repo-init**          | Install GitHub CLI, authenticate, initialize submodules, install locked platform dependencies, configure forks and remote topology | After first clone                                          |
| **npu-fleet-monitor**  | Build, start, inspect, or stop the local NPU dashboard from the standalone vaws-top repository | When continuously monitoring fleet resources and history  |
| **modelscope**       | Download, resume, status-check, and SHA256-verify ModelScope model weights                  | When model weights need to be downloaded into an explicit local directory |
| **vllm-ascend-serving** | Launch a vLLM Ascend inference service on a remote container, through coordinator-owned execution | When you need an inference service on a remote machine |
| **vllm-ascend-benchmark** | Run `vllm bench serve` performance benchmarks on a remote container, with multi-run warmup and statistical aggregation | When you need throughput/latency benchmarks or performance regression checks |
| **ascend-memory-profiling** | Profile and attribute HBM memory usage on Ascend NPU, with per-component breakdown and evidence chains | When you need to analyze memory consumption of a vLLM serving workload |
| **ascend-profiling-collection** | Collect Ascend torch-profiler data: start service, bracket profile window, run workload, remote analyse, and write a manifest | When you need kernel_details/trace_view captures |
| **ascend-profiling-analysis** | Analyze collected profiler roots/manifests and generate step/layer/operator/cross-rank reports | When you need to analyze profiling output |
| **vllm-ascend-graph-debug** | Diagnose graph compile, capture, replay, and graph/eager correctness divergence | When graph mode fails or diverges from eager mode |
| **vllm-ascend-correctness-validation** | Compare baseline/candidate, eager/graph, offline/online, and AISBench correctness | When validating accuracy or normalized outputs |
| **vllm-ascend-change-validation** | Derive validation plans from code diffs and aggregate PR evidence | When validating a workspace change or PR |
| **vllm-ascend-performance-regression** | Run alternating A/B experiments and assess variance and regression thresholds | When deciding whether throughput or latency regressed |
| **vllm-ascend-distributed-debug** | Diagnose topology, endpoint, collective, and per-rank distributed failures | When a failure depends on ranks, nodes, or parallel topology |
| **ascend-tensor-dump** | Capture bounded intermediate tensor dumps and locate the first divergent stage, in eager or graph mode | When output is wrong or two configurations disagree and the divergence must be localized |
| **ascend-operator-debug** | Reduce a model failure to one operator and run an explicit input/mode matrix | When building an isolated operator reproducer |
| **ascend-triton-operator-development** | Produce a first correct Ascend Triton candidate from PyTorch or GPU Triton semantics | When creating or migrating a Triton operator |
| **ascend-triton-kernel-validation** | Detect PyTorch fallback and execute an explicit correctness matrix | When validating an Ascend Triton candidate |
| **ascend-triton-kernel-optimization** | Run profiler-driven optimization after correctness gates pass | When tuning a correct Ascend Triton kernel |
| **ascend-triton-workflow** | Orchestrate development, validation, optimization, and Run Manifest evidence | When delivering an end-to-end Triton operator workflow |
| **vllm-ascend-pd-serving** | Start and observe one prefill/decode topology with HTTP smoke checks | When deploying disaggregated PD serving |

Skill selection follows the task. Detailed inputs and procedures live beside the relevant `SKILL.md`; ordinary local files and Git use native tools. [AGENTS.md](AGENTS.md) is the client entry and [the documentation index](docs/README.md) separates current contracts from dated evidence.

## Repository and local state

The canonical repository is `vllm-ascend-workspace/vllm-ascend-workspace`. Git submodules `vllm/` and `vllm-ascend/` remain on their community upstreams. Personal forks are development remotes; setup preserves established remote choices.

`.agents/skills/` contains business skills, `.agents/lib/` contains shared consumer code, and `.agents/scripts/` contains client wiring and maintenance tools. Client projections route to canonical skills. Runtime state and private configuration stay under untracked `.vaws-local/`; credentials are never committed. Public knowledge uses only package-prepared redacted copies.

The workspace license is independent of the submodules, which retain their upstream licenses.
