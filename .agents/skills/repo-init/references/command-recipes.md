# Repo-init command recipes

These are fallback patterns. Prefer the helper scripts when possible.

## Probe

```bash
python3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact
```

## Workspace machine profile

Inspect the local profile:

```bash
python3 .agents/scripts/workspace_profile.py summary
```

Create it with an explicit user-chosen machine username:

```bash
python3 .agents/scripts/workspace_profile.py ensure --username alice123
```

Create it with an auto-generated default:

```bash
python3 .agents/scripts/workspace_profile.py ensure
```

## Quiet remote comparison

Compare only `main` heads without broad pruning:

```bash
git -C vllm ls-remote --heads origin main
git -C vllm ls-remote --heads upstream main
```

Or use the helper:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm
```

## Configure remotes

Helper first:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure \
  --repo vllm \
  --origin-url git@github.com:USER/vllm.git \
  --upstream-url git@github.com:vllm-project/vllm.git
```

Raw fallback:

```bash
git -C vllm remote set-url origin git@github.com:USER/vllm.git
git -C vllm remote add upstream git@github.com:vllm-project/vllm.git
```

## Recursive submodules

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Ensure local `main` tracking quietly

Helper first:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main \
  --repo vllm \
  --remote origin
```

Raw fallback:

```bash
git -C vllm fetch --quiet --no-tags origin refs/heads/main:refs/remotes/origin/main
git -C vllm switch main || git -C vllm switch -c main --track origin/main
git -C vllm branch --set-upstream-to=origin/main main
git -C vllm pull --ff-only
```

## Sync a fork only with approval

```bash
gh repo sync USER/vllm --source vllm-project/vllm
gh repo sync USER/vllm-ascend --source vllm-project/vllm-ascend
```

## Set default PR target repo

```bash
gh repo set-default upstream
```
