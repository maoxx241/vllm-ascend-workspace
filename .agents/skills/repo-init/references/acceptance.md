# Repo-init acceptance criteria

## Trigger examples

These should trigger `repo-init`:

- “Initialize this workspace.”
- “Set up GitHub CLI and sign me in.”
- “Initialize submodules and point `vllm-ascend` to my fork.”
- “Bootstrap the four external dependencies.”
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
- asks only for missing choices that affect the result; reuses existing config after authorized init
- allows partial completion
- never writes personal remotes, secrets, or machine profile state into tracked files
- preserves extra remotes
- silently creates one persistent UUID4 in `.vaws-local/workspace-identity.json` during broad init

### Decision checkpoint

- for broad init, the skill stops after the first probe summary only for missing choices:
  - unified alias choice if the identity decision is pending
  - machine username choice if the profile is missing
- authorized init defaults to keep-current remotes, submodule init, `uv run --no-project python .agents/scripts/vaws_deps.py sync`, and CI-pinned vllm alignment unless the user overrides them
- when a machine username is missing, the helper suggests three options:
  - `git-username`
  - `random`
  - `custom`
- the skill does not silently assume a generated username for broad init
- the skill does not silently apply the recommended topology when the user only asked for generic init
- if the user picks `custom` without a literal username, the skill asks for the missing value; an already supplied value is used directly
- the skill does not silently replace `custom` with the detected Git username
- alias choices are `machine-username`, `custom`, and `none`; `custom` requires a literal value, asking only if missing
- choosing `none` persists `alias_decision=declined` and prevents repeated prompts

### Local machine profile

- broad workspace init reuses or creates `.vaws-local/machine-profile.json`
- machine usernames accept English letters and digits only
- profile creation normalizes usernames to lowercase
- random/default creation uses the `agent#####` format
- `repo_init_profile.py plan` returns suggested choices when the profile is missing
- `repo_init_profile.py apply --choice custom` without `--custom-username` returns `needs_input`
- narrow Git-only tasks do not force profile creation
- repeated identity initialization preserves the same UUID4

### Tooling and auth

- chooses the correct platform install path for `gh`
- offers a no-admin fallback when needed
- verifies GitHub auth after login
- asks before generating or uploading SSH keys

### Submodules and topology

- initializes submodules recursively for authorized broad initialization or an explicit submodule request
- resolves CI-pinned vLLM alignment with `resolve_vllm_ci_pin.py`, preferring `vllm-ascend/.github/vllm-main-verified.commit` over older workflow/docs fallbacks
- completes submodule init before configuring submodule remotes
- `repo_topology.py configure --repo <submodule>` errors out when the submodule is not initialized (git root mismatch)
- preserves nonstandard remotes
- keeps tracked files on community URLs
- classifies matching `{login}/vllm` and `{login}/vllm-ascend` as personal forks, distinct from `vllm-project/*` community upstream
- reports the resolved repository identity when a legacy personal GitHub URL redirects, instead of fabricating a new personal fork
- does not assume push access or silently select a personal fork because it exists
- uses `repo_topology.py configure` only for explicit fresh setup, not as an automatic transfer migration of established remotes
- uses quiet remote comparison instead of broad prune-heavy fetches
- moves local branches only with approval when worktrees are clean enough

### External dependencies

- successful dependency sync invokes installed knowledge preparation and reports model/index readiness separately from package installation
- default knowledge setup is local with shared downloads; existing publishing configuration is preserved and only explicit `--contribute` enables a corpus fork
- a read-only setup disables contribution while keeping shared sync, without requiring a fork
- MCP maintains knowledge while alive without an Agent maintenance sequence
- Windows/WSL clients of one Windows-mounted workspace share its Windows knowledge process; a missing Windows interpreter is pending, and an independent Linux workspace remains native
- supported final-response hooks save the existing summary without reading a complete transcript or starting a second summarization model
- repeated setup preserves foreign MCP/hook configuration and does not bypass native hook trust
- knowledge PRs use human review and merge; ordinary development requires no fork or publishing follow-up
- lookup and capture remain optional; unavailable knowledge does not block independent work, and authors use ordinary Markdown without a required schema

- offers `uv run --no-project python .agents/scripts/vaws_deps.py sync` as a required step for package-dependent work
- skipping `uv run --no-project python .agents/scripts/vaws_deps.py sync` still completes `repo-init` for local documentation and Git work
- the three in-process packages are public git+https installs; `uv.lock` is the only pin
- after install or skip, runs `uv run --no-project python .agents/scripts/vaws_deps.py doctor`
- the finish summary names available and unavailable capabilities from `doctor`'s report, without re-deriving them

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`
- `.agents/skills/repo-init/scripts/_profile_choice_common.py`
- `.agents/skills/repo-init/scripts/repo_init_profile.py`
- `.agents/skills/repo-init/scripts/repo_init_probe.py`
- `.agents/skills/repo-init/scripts/repo_topology.py`
- `.agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py`
- `.agents/scripts/vaws_deps.py`
- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`
