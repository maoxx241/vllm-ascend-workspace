# vllm-ascend-workspace

**[中文](README.md)** | **English**

A composable local development scaffold for working on [vLLM](https://github.com/vllm-project/vllm) and [vLLM Ascend Plugin](https://github.com/vllm-project/vllm-ascend) in a single workspace, with built-in AI Agent skills for automated environment setup, remote NPU machine management, and code synchronization.

## What problem does this solve

Developing vLLM Ascend typically involves editing code locally, running tests on remote Ascend NPU servers, and tracking upstream vLLM changes — all of which require repetitive Git, SSH, and environment configuration.

`vllm-ascend-workspace` wraps these operations into three AI Agent skills. You can ask an Agent to handle them in natural language, or ignore the skills entirely and use it as a plain multi-repo workspace.

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


All skills are **optional**. Use any subset, or none at all.

## Usage examples

When talking to an Agent:

```
# Initialization
"Run repo-init and set me up for PR work."
"Only install GitHub CLI and log me in. Do not touch remotes."
"Initialize in community-only mode. Do not create any forks."

# Machine management
"Configure this NPU machine for the current workspace."
"Check whether the managed machine is ready."
"Repair the container SSH on the managed host."

# Code sync and remote execution
"Sync my latest local code before running tests on the remote machine."
```

## Repository layout

```
.
├── vllm/                  # Upstream vLLM (Git submodule)
├── vllm-ascend/           # vLLM Ascend Plugin (Git submodule)
├── .agents/
│   ├── skills/
│   │   ├── repo-init/         # Workspace initialization skill
│   │   ├── machine-management/# Remote machine management skill
│   │   └── remote-code-parity/# Code synchronization skill
│   ├── lib/               # Shared local-state library
│   └── scripts/           # Shared helper scripts
├── .cursor/rules/         # Cursor IDE specific rules
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


| File             | Tools covered                                                          |
| ---------------- | ---------------------------------------------------------------------- |
| `AGENTS.md`      | Codex CLI, GitHub Copilot, Cursor, Windsurf, Cline, Devin, Aider, etc. |
| `CLAUDE.md`      | Claude Code                                                            |
| `.cursor/rules/` | Cursor                                                                 |


## License

This scaffold repository is licensed independently from its submodules. `vllm/` and `vllm-ascend/` each follow their respective upstream licenses.