# vllm-ascend-workspace

**[中文](README.md)** | **English**

A composable local development scaffold for working on [vLLM](https://github.com/vllm-project/vllm) and [vLLM Ascend Plugin](https://github.com/vllm-project/vllm-ascend) in a single workspace, with built-in AI Agent skills for automated environment setup, remote NPU machine management, and code synchronization.

## What problem does this solve

Developing vLLM Ascend typically involves editing code locally, running tests on remote Ascend NPU servers, and tracking upstream vLLM changes — all of which require repetitive Git, SSH, and environment configuration.

`vllm-ascend-workspace` wraps these operations into six AI Agent skills. You can ask an Agent to handle them in natural language, or ignore the skills entirely and use it as a plain multi-repo workspace.

## Quick start

```bash
# Clone the repository
git clone https://github.com/maoxx241/vllm-ascend-workspace.git
cd vllm-ascend-workspace

# Initialize submodules
git submodule update --init --recursive
```

If you use an Agent-capable IDE (Cursor, Windsurf, etc.) or terminal tool (Claude Code, Codex CLI, etc.), you can complete the rest of the setup in natural language:

> "Initialize this workspace and set me up for vLLM Ascend development."

The Agent will detect your environment, install required tools, and configure Git remotes and forks.

## Built-in skills


| Skill                  | Purpose                                                                                      | When to use                                                |
| ---------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **repo-init**          | Install GitHub CLI, authenticate, initialize submodules, configure forks and remote topology | After first clone                                          |
| **machine-management** | Add, verify, repair, or remove a remote Ascend NPU server and its managed container          | When setting up a remote NPU dev machine                   |
| **remote-code-parity** | Sync the full local workspace state (including uncommitted changes) to a remote container    | Triggered automatically before remote test or service runs |
| **vllm-ascend-serving** | Launch a vLLM Ascend inference service on a remote container, with NPU probing, auto card selection, and incremental restart | When you need an inference service on a remote machine |
| **vllm-ascend-benchmark** | Run `vllm bench serve` performance benchmarks on a remote container, with multi-run warmup and statistical aggregation | When you need throughput/latency benchmarks or performance regression checks |
| **ascend-memory-profiling** | Profile and attribute HBM memory usage on Ascend NPU, with per-component breakdown and evidence chains | When you need to analyze memory consumption of a vLLM serving workload |


All skills are **optional**. Use any subset, or none at all.

## Usage examples

When talking to an Agent:

```
# Initialization
"Help me initialize this repo"
"Help me set up this repo"

# Machine management
"Add these two servers, IPs are x.x.x.1 and x.x.x.2, password is xxxx"
"Set up this server for me, IP is x.x.x.x, password is xxxx"
"Remove the x.x.x.x server"

# Code sync
"Sync my code to the server and rebuild"

# Serving (--model must point to weights already present on the remote container)
"Launch a 4-card inference service on x.x.x.x using /home/weights/Qwen3-32B-W8A8 with W8A8 quantization"
"Check the service status on x.x.x.x"
"Stop the service on x.x.x.x"

# Benchmarking
"Run a benchmark on x.x.x.x with Qwen3.5-35B, 4 cards, 5 runs keep last 4"
"Compare throughput between main and this PR"
```

## Repository layout

```
.
├── vllm/                  # Upstream vLLM (Git submodule)
├── vllm-ascend/           # vLLM Ascend Plugin (Git submodule)
├── .agents/
│   ├── skills/
│   │   ├── repo-init/         # Workspace initialization skill
│   │   ├── machine-management/    # Remote machine management skill
│   │   ├── remote-code-parity/    # Code synchronization skill
│   │   ├── vllm-ascend-serving/   # Inference serving skill
│   │   ├── vllm-ascend-benchmark/ # Performance benchmarking skill
│   │   └── ascend-memory-profiling/ # Memory profiling skill
│   ├── lib/               # Shared local-state library
│   └── scripts/           # Shared helper scripts
├── .cursor/rules/         # Cursor IDE specific rules
├── .trae/                 # TRAE IDE specific rules and skills
├── AGENTS.md              # Cross-tool Agent instructions (Agents read this)
├── CLAUDE.md              # Claude Code instruction entry point
└── README.md              # Chinese README (default)
```

## Design principles

- **Nothing is mandatory** — All skills are optional. Developers choose what to use.
- **Local state stays untracked** — User-specific remotes, auth, and machine config live only in the untracked `.vaws-local/` directory.
- **Submodules point to community** — `.gitmodules` always targets `vllm-project` official repos. Personal forks are a local runtime concern.
- **Agent-driven, not Agent-dependent** — Everything can be done manually. Agent skills just make it more convenient.

## Recommended remote topology

Skills recommend the following topology, but never enforce it:


| Repository    | `origin`             | `upstream`                       |
| ------------- | -------------------- | -------------------------------- |
| workspace     | Your fork (optional) | `maoxx241/vllm-ascend-workspace` |
| `vllm`        | Your fork (optional) | `vllm-project/vllm`              |
| `vllm-ascend` | Your fork            | `vllm-project/vllm-ascend`       |


## Multi-tool support

This repository supports mainstream AI coding tools:


| File             | Tools covered                                     |
| ---------------- | ------------------------------------------------- |
| `AGENTS.md`      | Codex CLI, GitHub Copilot, Cursor, TRAE, OpenCode |
| `CLAUDE.md`      | Claude Code                                       |
| `.cursor/rules/` | Cursor                                            |
| `.trae/`         | TRAE                                              |


## Roadmap

### Done

- **repo-init** — Workspace initialization: GitHub CLI install, auth, submodules, fork & remote topology
- **machine-management** — Remote machine management: add, verify, repair, remove Ascend NPU servers and managed containers
- **remote-code-parity** — Code sync: push full local workspace state (including uncommitted changes) to remote containers
- **vllm-ascend-serving** — Service launch: idle NPU detection, idle port detection, one-click vLLM Ascend inference serving
- **vllm-ascend-benchmark** — Online performance benchmarking: single-run / multi-run (warm-service) mode, warmup exclusion, statistical aggregation; multi-state regression comparisons orchestrated by the Agent
- **ascend-memory-profiling** — Memory profiling: collect and analyze HBM usage, per-component breakdown (fixed overhead, weights, KV cache, HCCL, activations, runtime), with msprof component-level attribution

### Planned

- **Accuracy testing & aisbench integration** — Automated evaluation based on aisbench, with HTML report analysis, system scheduling assessment, and DP balance analysis
- **Performance profiling** — Automatic operator latency breakdown, hot operator AIC/AIV/MTE2 ratio analysis, AICPU operator identification, host bound detection and diagnosis
- **Sync-break optimization** — Provide async copy overlap strategies for specific cases to reduce synchronization overhead
- **Compute graph analysis** — Build model compute graphs, generate theoretical performance evaluation reports and optimization recommendations
- **External knowledge base** — Integrate external knowledge sources to extend Agent capabilities

## License

This scaffold repository is licensed independently from its submodules. `vllm/` and `vllm-ascend/` each follow their respective upstream licenses.