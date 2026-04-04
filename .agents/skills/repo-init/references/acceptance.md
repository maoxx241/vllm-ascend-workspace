# Repo-init acceptance criteria

## Trigger examples

These should trigger `repo-init`:

- “Initialize this workspace.”
- “Set up GitHub CLI and sign me in.”
- “Initialize submodules and point `vllm-ascend` to my fork.”
- “Configure my remotes for PR work.”
- “初始化这个仓库，顺便把后面远端机器要用的用户名也配好。”

## Non-trigger examples

These should not trigger `repo-init` unless setup is the obvious blocker:

- “Fix this failing test.”
- “Explain how scheduling works.”
- “Run the benchmark suite.”
- “Update README wording.”
- “帮我配置一台远端 NPU 机器。”

## Success criteria

A successful run should satisfy all applicable items below.

### Universal

- probes first before mutating
- asks before each mutation category
- allows partial completion
- never writes personal remotes, secrets, or machine profile state into tracked files
- preserves extra remotes

### Local machine profile

- broad workspace init reuses or creates `.vaws-local/machine-profile.json`
- machine usernames accept English letters and digits only
- profile creation normalizes usernames to lowercase
- blank input generates a default machine username
- narrow Git-only tasks do not force profile creation

### Tooling and auth

- chooses the correct platform install path for `gh`
- offers a no-admin fallback when needed
- verifies GitHub auth after login
- asks before generating or uploading SSH keys

### Submodules and topology

- initializes submodules recursively
- preserves nonstandard remotes
- keeps tracked files on community URLs
- uses quiet remote comparison instead of broad prune-heavy fetches
- moves local branches only with approval when worktrees are clean enough

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`
- `.agents/skills/repo-init/scripts/repo_init_probe.py`
- `.agents/skills/repo-init/scripts/repo_topology.py`
- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`
