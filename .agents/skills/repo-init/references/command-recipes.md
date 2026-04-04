# Repo-init command recipes

Prefer the helper scripts in `scripts/` and `.agents/scripts/` when possible.

## Probe

macOS / Linux / WSL:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact
```

Windows:

```powershell
py -3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact
```

## Local machine profile

Validate one user-provided name:

```bash
python3 .agents/scripts/workspace_profile.py validate alice123
```

Create a specific profile after the user chose a name:

```bash
python3 .agents/scripts/workspace_profile.py ensure --username alice123
```

Create a default/random profile only after the user explicitly accepted that option:

```bash
python3 .agents/scripts/workspace_profile.py ensure --generate
```

## Submodules

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Quiet main comparison

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo .
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm-ascend
```

## Remote configuration

Workspace example:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure \
  --repo . \
  --origin-url git@github.com:USER/vllm-ascend-workspace.git \
  --upstream-url git@github.com:maoxx241/vllm-ascend-workspace.git
```

`vllm-ascend` example:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure \
  --repo vllm-ascend \
  --origin-url git@github.com:USER/vllm-ascend.git \
  --upstream-url git@github.com:vllm-project/vllm-ascend.git
```

## Branch tracking

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main \
  --repo vllm-ascend \
  --remote origin
```
