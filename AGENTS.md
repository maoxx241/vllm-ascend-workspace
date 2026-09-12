# Repository instructions

This is the vLLM-Ascend consumer workspace: project materials, client wiring
and business skills. Runtime owners are remote-dev, vaws-coordinator,
vaws-knowledge and vaws-top. See [docs/target-state.md](docs/target-state.md).

Design decisions follow the nine [design principles](docs/design-principles.md),
with total cost of achieving the user's actual goal taking priority. Use tools
for bounded operations, keep open judgment with the Agent, and treat knowledge
as reference. Reuse valid work and internalize routine checks and recordkeeping.
Capabilities participate on demand; these principles add no per-task checklist.
Current execution entries are listed below.

The canonical repository is `vllm-ascend-workspace/vllm-ascend-workspace`.
`vllm/` and `vllm-ascend/` are Git submodules; keep `.gitmodules` on
`vllm-project/vllm` and `vllm-project/vllm-ascend`. Personal forks are development
remotes, not replacements for community upstreams.

## First use, forks and updates

This applies without invoking a Skill. On first use, if no confirmed
`.vaws-local/github.json` exists, inspect `.agents/scripts/workspace_forks.py`
and ask once for the user's personal GitHub username, explaining that setup
creates personal development forks and enables upstream update preparation. The current
authenticated login is a suggestion, not consent. Reuse an explicit answer;
do not infer identity from the OS account or remotes. Continue independent
local/read-only work while the answer is pending; a deferred choice is not a
repeated task gate.

After the user accepts setup, use `uv run --no-project python
.agents/scripts/workspace_forks.py --github-user USER --apply`. The tool verifies
the authenticated User and genuine upstream fork network, creates/reuses the
personal forks, and sets personal `origin` and canonical `upstream`. All
development forks, including optional component and knowledge contribution
forks, must belong to personal GitHub Users. Organization forks, redirected
names and unrelated same-name repositories do not qualify. Canonical project
repositories remain upstreams; `.gitmodules` keeps community URLs.

Native CLI/session entries start configured upstream preparation without waiting
for downloads. The background watcher never changes an existing editing
checkout or running environment. It periodically tracks the canonical default
branch and its pinned components; no Release is required. New CLI editing copies
can use a prepared revision. Explicit maintenance of an existing checkout can use
`.agents/scripts/workspace_update.py apply`; this is not a per-task Agent step.
Dirty sources and divergence stay for judgment when an update is needed.
See [forks and updates](docs/forks-and-updates.md).

Servers use shared root access. The coordinator binds the
initialized GitHub user to a fixed `vaws-<github-login>` container, with naming,
ownership records, notifications and reuse checks handled inside the packages.
Do not add per-task identity fields, container selection, inbox polling or
bookkeeping to Agent workflows. Native task ownership still applies; messages
do not grant control over someone else's execution. Optional messages use
`vaws_message` with a returned coordination reference and text; incoming messages
arrive through normal task calls without Agent polling. See
[identity and coordination](docs/identity-and-agent-coordination.md). On shared
development servers, reuse existing operator builds, weights and compatible
environments regardless of creator. Tools check relevant compatibility without
adding personal/public categories, sharing permissions or publication steps.

## Choose the execution owner

| Task | Entry |
|---|---|
| Local files, shell, Git | Native client tools |
| Explicit remote endpoint I/O | remote-dev companion tools with ordinary host/port/user/cwd |
| Managed environments, NPU runs and services | `vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish` |
| Workspace initialization or client wiring | `.agents/skills/repo-init/SKILL.md` |
| Start a native CLI in an independent editing directory | `.agents/scripts/vaws_client.py CLIENT` |
| Local fleet monitor lifecycle | `.agents/skills/npu-fleet-monitor/SKILL.md`; observation is not allocation |
| Knowledge lookup and capture | `knowledge_query`, `knowledge_explain`, `knowledge_capture` |

Bind default business worktrees with `vaws_session(sources=...)`, or pass
`sources` to one `vaws_run`; an empty map runs without project sources.
Admission fixes source inputs for each execution. Coordinator
prepares managed sources, environments, devices and ports through one `run`.
Read status, tail or stop an owned execution through its package reference;
a live service does not require acquiring the same NPUs again. Explicit
source-only publication is an optional package path described in
[docs/coordinator-consumption.md](docs/coordinator-consumption.md).

Task identity comes from the native attachment's `context_file` or
`VAWS_CONTEXT_FILE`. New native sessions create new tasks; resume keeps the
original. Joining another task requires explicit association. Never infer
identity or resource access from cwd, recent chats or a local report.

Do not create per-task containers, local NPU leases, workspace request/recovery
ledgers, or remote-dev resolver plugins. Shared resources and their ownership
remain with the packages. Preserve live containers and unrelated worktrees.

## Skills and knowledge

Repo-local skills under `.agents/skills/` add business judgment and convenient
workflows. Read the selected `SKILL.md` and only the references needed for the
current task. Ordinary coding, docs and Git operations need no management skill.
Detailed tool arguments belong to package help and the linked documentation.

Knowledge is optional reference. Query when experience could help; lookup and
capture are not task prerequisites or completion steps. Use current evidence and
judgment. Missing or unavailable knowledge does not block work; a search miss
does not prove absence. A Markdown title and body are enough: keep known
conditions, evidence and uncertainty in the text without a schema. Configured
hooks reuse the normal summary; manual capture can reuse useful existing text.
No second summary or publishing follow-up is required. Storage and maintenance
details are in the [knowledge contract](docs/target-state.md#54-knowledge).
For explicit knowledge maintenance, the optional package skill is available
through its configured interpreter with `python -m vaws_knowledge skill`.

Only a package-prepared redacted copy may be contributed publicly. Internal
addresses, user paths, hostnames, container identifiers and credentials must
not leave the local source. Contribution configuration and shared updates use
`.agents/scripts/knowledge_setup.py`; public review and merge are currently
human. Native client hook trust is not granted by setup.

## Verification and maintenance

Use `uv run --no-project python .agents/scripts/vaws_deps.py doctor` to inspect installed
capabilities; `uv run --no-project python .agents/scripts/vaws_deps.py sync` prepares or reuses
an immutable environment from `pyproject.toml` and `uv.lock`. vaws-top is a
separate uvx service. Pin drift is reported, not a new execution gate.

Pure Python control-plane, configuration and documentation checks run locally.
`torch`/`torch_npu`/vLLM device execution runs in a remote Ascend container.
Run checks affected by the change; existing evidence can support a conclusion
without recreating a plan or repeating unrelated experiments.

The same `uv run --no-project python` prefix works in PowerShell, bash and zsh.
Native paths, process ownership and managed Windows/WSL owner selection stay
inside the tools; see [docs/platform-contract.md](docs/platform-contract.md).

Managed executions record their fixed inputs and lifecycle automatically;
cross-workflow business measurements use Run Manifest v1 from
`vaws_coordinator.run_manifest`, saved by tools under untracked `.vaws-local/`.
Tools record execution facts; agents do not fill management records manually. Skill scripts put progress on stderr and their
result on stdout. Keep runtime state under untracked `.vaws-local/`, including
`remote-dev-state/` and `agent-sessions/`. Never track credentials.

When a skill changes, update its scripts, references, metadata, client
projections and affected callers together. Package behavior stays with its
owner. `docs/` documents carry a `Status:` line: current is a contract, dated
is historical evidence. See [docs/README.md](docs/README.md).
