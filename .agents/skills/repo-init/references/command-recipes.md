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

## Broad-init machine profile

Get the exact three-option machine-username question:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_profile.py plan
```

Apply the Git-username option:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice git-username
```

Apply the random `agent#####` option:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice random
```

Apply the custom option after the user gave the literal username:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice custom --custom-username alice123
```

## Low-level profile helper

Validate one user-provided name:

```bash
python3 .agents/scripts/workspace_profile.py validate alice123
```

Read the current profile summary:

```bash
python3 .agents/scripts/workspace_profile.py summary
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
python3 .agents/skills/repo-init/scripts/repo_topology.py configure   --repo .   --origin-url git@github.com:USER/vllm-ascend-workspace.git   --upstream-url git@github.com:maoxx241/vllm-ascend-workspace.git
```

`vllm-ascend` example:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure   --repo vllm-ascend   --origin-url git@github.com:USER/vllm-ascend.git   --upstream-url git@github.com:vllm-project/vllm-ascend.git
```

Optionally set `gh repo set-default` during configure:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure   --repo vllm-ascend   --origin-url git@github.com:USER/vllm-ascend.git   --upstream-url git@github.com:vllm-project/vllm-ascend.git   --gh-default upstream
```

## Branch tracking

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main   --repo vllm-ascend   --remote origin
```
