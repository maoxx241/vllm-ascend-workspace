# Native client validation — 2026-09-12

Status: dated validation evidence, 2026-09-12

This records native client startup and task association, using local Git
worktrees, fixtures and the already signed-in clients. It does not establish remote NPU
execution or support for untested client versions. Raw transcripts and local
identifiers remain under untracked `.vaws-local/implementation/`.

## Claude Code

Native `--worktree` startup, ordinary model tools and resume passed. Three new
native sessions acquired different VAWS tasks. Calls to `vaws_session` supplied
no arguments: native hooks selected the task and bound the actual editing
directory. The tasks read a fixture README and inspected their cwd, Git HEAD
and shell environment; they created no managed executions.

The initial client was 2.1.143. An official `claude update` installed 2.1.269
while retaining the existing login and settings. Tests used the configured
model/provider and ordinary client permissions; they did not change permission
defaults or use a VAWS command to create each task.

| Case | Observed result |
| --- | --- |
| New native worktree on 2.1.143 | Editing cwd, automatic source binding and VAWS task agreed. |
| Source MCP configuration selected an older environment | All three VAWS MCP providers launched in the newly prepared target environment. |
| Resume from the existing target on 2.1.143 | Native session, VAWS task, source and selected environment were preserved. |
| Resume from the mother checkout on 2.1.143 | The client could not locate the conversation; no model task ran. |
| Resume from the mother checkout on 2.1.269 | The client returned to the original target and retained both identities. |
| New native worktree on 2.1.269 | A distinct VAWS task and the new editing directory were selected automatically. |

Before resume, the fixture mother checkout advanced and the target `uv.lock`
was deliberately modified. Resume preserved the target HEAD, dirty lock file
and saved environment receipt. It did not rerun worktree preparation.

An ordering probe established that this client's MCP configuration was read
before `WorktreeCreate`, although the MCP process cwd changed after creation.
The fixed-provider entry adapter therefore resolves the prepared receipt from
the actual cwd when it starts. It does not prepare dependencies, update Git or
infer task identity. `SessionStart` uses Claude's `CLAUDE_ENV_FILE` channel to
make the selected environment and task context available to ordinary shell
tools. The hook contract and native worktree behavior are described in the
[Claude hook reference](https://code.claude.com/docs/en/hooks#worktreecreate)
and [worktree documentation](https://code.claude.com/docs/en/worktrees).

Affected Python regression groups passed with **92 passed, 4 skipped and
11 subtests passed**. They cover native attachments, existing client config
merging, knowledge hooks and the new Claude adapter. The result is specific to
the tested platforms and cases; the skipped tests are not runtime acceptance.

Evidence is under `.vaws-local/implementation/20260912-native-claude/`:

- `summary.json`: session results, actual tool inputs and preservation checks.
- `order-events.jsonl`: MCP configuration and cwd ordering probe.
- `full-fixture/.claude/worktrees/full-two-debug.log`: selected interpreter
  evidence for all three providers after starting with an older bootstrap.
- `resume-before.json`, `resume-updated-result.jsonl` and
  `resume-updated-debug.log`: unchanged target state and mother-checkout resume.
- `latest-new-result.jsonl` and `latest-new-debug.log`: new session on 2.1.269.
- `affected-tests.log`: regression result.

The fixture disabled upstream fetching to isolate native client integration.
This test does not replace the separate default-branch update acceptance. A
custom user `WorktreeCreate` hook remains owned by that user and is preserved
by setup; its behavior is outside this adapter's acceptance.

## Cursor

Cursor 3.19.19 was tested through its real Agents GUI. Initialization selected
New Worktree as the default environment and installed the three VAWS providers
in the user MCP configuration with `--cursor-global-mcp`. Existing custom
servers and native approval settings were preserved.

The first attempts exposed a real configuration gap: enabling a project MCP
server in the mother checkout did not enable the separately identified server
in a new worktree. User-level providers have stable native identifiers. Their
`${workspaceFolder}` environment value is resolved separately for each worktree;
the fixed-provider launcher reads that target's prepared environment receipt.
New directories consequently do not require repeated MCP configuration.

Two ordinary GUI-created sessions acquired different editing directories and
VAWS tasks. Both called `vaws_session` without arguments. The first wrote and
read an ignored acceptance file; the second reported that file absent. Shell
`TaskClient()` also resolved the first session through the native
`CURSOR_CONVERSATION_ID`, without an explicit context argument or environment
activation. The MCP result, shell result, actual pwd and source HEAD agreed.
No managed execution or remote host was involved. Returning to the first GUI
conversation retained its file, VAWS task, cwd, HEAD and environment key after
the mother checkout had advanced; its receipt hash was unchanged.

The selected dependency environment remained fixed in each directory. These
real tasks used workspace `1cd163c` and coordinator `dbff0ef`; later consumer
changes do not retroactively change that acceptance baseline. Local evidence
is under `.vaws-local/implementation/20260912-shared-root/native-cursor-live/`.

## Grok

The official 1.0.30 client was tested first. Ordinary native Git worktree
startup and no-argument VAWS association worked, but `/new` and `/fork` inside
a detached worktree reused the editing directory. A narrow
[personal client patch](https://github.com/maoxx241/grok-build/commit/79bd6df3d6d568b71bca3500bf0a3dd3d54db15d)
includes `session.is_worktree` in those two directory decisions. The public
source identifies itself as 1.0.24; the installed patched version is therefore
**1.0.24 (79bd6df3d6d5)**, not an official 1.0.30 build.

Real ordinary startup, `/new` and `/fork` produced three distinct worktrees;
each model's `pwd` agreed with its native attachment. The normal `grok` entry
was then switched to the built personal version and tested with the existing
global profile. A local read-only task and resume both passed. The native ID,
VAWS task, cwd, lock file, generated configuration and selected receipt stayed
the same on resume. The full MCP result was visible to the client through the
text result as well as structured content.

Two native regression cases and nine workspace adapter cases passed. Global
configuration, trust and authentication files had unchanged hashes. The
previous official binary was retained; the installation receipt records its
original link and the exact rollback command. No periodic updater was added.
Evidence is under
`.vaws-local/implementation/20260912-shared-root/grok-native-live/`:
`global-entry-summary.json`, `global-install.json`, the native transcript and
resume output. The normal-entry acceptance used workspace `1cd163c` and
coordinator `dbff0ef`.

## Kimi Code

The local Kimi extension adds `SessionSetup` before native workspace creation,
then uses the returned directory for session state and MCP loading. Native
session and agent IDs travel with MCP calls and ordinary shell commands;
existing `SessionStart` and subagent events perform VAWS attachment.

The tested client is a local extension of Kimi Code 0.42.0, built with Node 24.
It is not a claim that the unmodified released client supports `SessionSetup`.
The retained source and patch are under
`.vaws-local/implementation/20260912-shared-root/kimi-code-source/`.

The native client completed these local tasks:

| Case | Observed result |
| --- | --- |
| New session | Bash wrote a small proof file in the prepared editing directory, then Read opened it there. The shell carried the native session ID and `main` agent ID. |
| Explicit resume from the mother checkout | Read and Bash returned the original proof, cwd and native identities. |
| Native Agent delegation | The child read the README in the same task directory, carried the same session ID and a distinct real child agent ID. |

User-level MCP configuration supplies the fixed VAWS providers to new
workspaces. Real `vaws_session({})` calls passed in both a main session and its
native `coder` child: they selected the same VAWS task with different native
attachments. The native read-only `explore` profile does not expose MCP tools;
that is its existing profile capability. The standalone native build also
passed `-c` continuation with the original identities. An older session which
was created without MCP retains its original MCP baseline on resume.

The transcripts `create.jsonl`, `resume.jsonl`, `child.jsonl` and
`mcp-fresh.jsonl` are under
`.vaws-local/implementation/20260912-shared-root/kimi-validation/`.
The extended client is available in the
[personal source fork](https://github.com/maoxx241/kimi-code/tree/50d1d46e5a15a427453e811e28ad5ad60875cf24).
It was built with the project's native macOS build script, yielding a
standalone executable rather than a required Node launcher. The official
binary is retained for rollback. The official `upgrade.auto_install` preference
was set to false for this extension installation, with the prior value recorded.
Version notices and explicit `kimi update` remain available; installing an
official version replaces the extension. No update watcher was added.

The installed normal `kimi` entry also completed a fresh model task on workspace
`d580e1f` with coordinator `f635153`. Its no-argument MCP call and Bash cwd
agreed, exactly one native attachment was present, and no extra context text
was injected into the user prompt. It created an ordinary untracked proof file
for the following fork acceptance. The model/provider and native approval
settings were retained. Explicit-ID resume through the actual TUI, started from
the mother checkout, reopened the original worktree and its MCP providers.

Setup now retains the explicit extension choice even when Kimi's own settings
writer removes TOML comments. It replaces earlier generated hooks from the
same Git family and preserves custom hooks and independent projects. Three
end-to-end migration cases passed after the final parser adjustment. The six
hooks belonging to an older independent test fixture were removed separately
as test cleanup, with a local backup; the general migration does not remove
unrelated project hooks.

Independent review found three concrete lifecycle gaps: plain continue from
the mother checkout still used an exact cwd match; nonzero setup failures and
timeouts could silently fall back to the mother checkout; and the native child
start hook ran after the child turn had already launched. The follow-up patch
uses the native recent-session index for worktree-family continuation, reports
setup failures before materializing a session, and orders child attachment
before its turn. Independent focused checks passed: seven setup cases, one
real Git-family fixture including pagination, and one delayed-child-hook case
that proves the turn is not enqueued until attachment completes. The review
record is `kimi-independent-review.json` beside the Kimi source directory.

Native fork preparation also preserves staged, unstaged and ordinary untracked
files, including initialized submodules, through the existing workspace-copy
implementation. It uses linked worktrees and retains the source HEAD and saved
environment, whether the source is clean or dirty. Forty-three affected Python
tests passed, including real Git fixtures and unchanged source-index checks.
This preservation applies only to conversation forks; ordinary new sessions
continue using startup update preparation.

Native SDK and file-history checks covered complete forks, forks at a selected
conversation turn, independent child sessions and cross-worktree resume. The
target has its own metadata, runtime binding and retained file-history blobs;
deleting the source session did not remove the fork's retained snapshots.
Directory preparation runs inside the existing source-agent pause, after
pending file history has settled. An active source is rejected before setup
creates a directory. The final affected results were 20 SDK, 5 file-history,
21 lifecycle and 69 TUI/family tests, with core and CLI type checks passing.

The final installed client at `50d1d46` passed a real TUI `/fork`, followed by
explicit-ID TUI resume from the mother directory. Its read-only model task
confirmed a new cwd and a new VAWS task through Bash and no-argument MCP.
The tracked README edit and ordinary untracked proof file matched the parent;
HEAD, index tree and environment matched, while the parent's files, diff,
index and HEAD remained unchanged. Evidence is `native-fork-filesystem.json`,
`native-tui-fork.raw` and `native-tui-fork-resume.raw` in the Kimi validation
directory. The installation receipt records the exact binary hash and official
backup under untracked `.vaws-local/client-installations/`.
The subsequent TUI `/new` created a clean independent directory and empty
history with all three providers connected. Returning to the original parent
by ID restored its worktree and history. All validation TUI processes exited
normally; the consolidated result is `kimi-final-validation.json` alongside
the raw transcripts.

## Codex hooks and local environment selection

A user-supplied Local session on workspace `360c876` exposed a separate
initialization failure: its VAWS MCP provider was available, but a no-argument
call failed because the native attachment did not exist. The installed desktop
runtime was Codex `0.154.0-alpha.6.2`. Its official `hooks/list` API reported six
enabled project hooks with `trustStatus=untrusted` and no configuration errors.
Codex requires native review of each new hook definition; merely writing a
configuration file does not authorize its execution. See the
[official hook documentation](https://learn.chatgpt.com/docs/hooks).

The `--codex-global-hooks` initialization option now installs a fixed user hook
for this Git worktree family. Its command and source remain stable when a new
directory selects another environment. The adapter uses the native payload's
actual cwd to read that directory's saved receipt and task-provider settings;
it neither creates a worktree nor guesses an identity. New-directory setup
automatically removes duplicate generated project hooks. Custom hooks and
inline user configuration remain intact. The optional Stop event uses the
same adapter for the selected knowledge component.

Real linked-worktree fixtures retained old environments, dirty lock files,
native IDs and custom registry settings. The installed runtime's `hooks/list`
confirmed identical keys and hashes across cwd changes, and a changed hash
when the command changed. Sixty-two affected consumer tests passed. The
legacy generated local-venv MCP configuration also migrated to the selected
immutable environment, removing only exact obsolete generated defaults while
preserving custom providers and settings.

For this authorized installation, the seven generated definitions were
reviewed through the native `hooks/list` and `config/batchWrite` API. Both
pre-existing hook trust entries were preserved. A native archive/unarchive
of the idle supplied session refreshed its provider from the old environment
to coordinator `f635153` and produced the SessionStart context automatically.
The conversation, cwd and HEAD were preserved. Raw installation, exact catalog
hashes, backups and protocol evidence remain under
`.vaws-local/implementation/20260912-shared-root/codex-hook-audit/`.

The refreshed session exposed a second gap in code-mode MCP calls. A temporary
byte-preserving stdio probe observed the native caller in
`_meta.x-codex-turn-metadata.thread_id`, with empty tool arguments. Coordinator
now reads that exact field and looks up the existing attachment. It does not
infer an identity from other metadata fields, cwd or the server process's
environment, and does not create an attachment from an MCP call. Seventy-four
affected package tests passed, including alternating native callers through
one server and the existing Kimi metadata behavior.

The final real call through `functions.exec` supplied `{}` and succeeded with
coordinator `fc3973c`. It selected the same VAWS task and context produced by
SessionStart, bound the actual main checkout, and reported zero executions.
Native shell cwd and HEAD agreed with the automatic source. Updating the
component preserved all seven native hook keys, hashes and trusted states.
The temporary provider configuration was restored and probe processes were
released; `user-session-calls.json` and the referenced package result retain
the actual requests and loaded revision. This establishes Local-session
association on the tested desktop version. The supplied session remains Local;
it does not establish selection of a native worktree environment.

The native `create_thread` worktree API can create a task without selecting a
local environment. Merely creating the local-environment TOML file does
not establish that selection for this installed app.

One real API-created worktree task read the local README and inspected its
cwd and HEAD on canonical main `b63c782`. Its native log recorded
`hasLocalEnvironment=false`, so setup was skipped and `vaws_session` was not
available. That is a recorded incomplete integration case, not a successful
native-environment acceptance.

Read-only inspection of the installed app established two separate values:

| Value | Role |
| --- | --- |
| Project environment selection | The app's `local-env-selections-by-workspace` preference maps the host and project path to the selected environment file. Native task creation reads this selection. |
| `codex.localEnvironmentConfigPath` Git key | The native creator writes the selected path, or `__none__`, into the new worktree's Git configuration. It records that worktree's selection. |

Writing the Git key in the source checkout therefore does not initialize the
new-session selection used by the app API. A null selection skips the setup
script, even when an environment file exists in the source tree. The supported
one-time operation is selecting the environment in the native app.

The tools available during this acceptance had no environment-selection
setter, and computer use explicitly disallowed operating the Codex interface.
No private bridge, application state file or database was modified to work
around that restriction. API worktree creation alone does not prove selection.
The user subsequently selected the native environment, as recorded below.

The code inspected was in the installed app's `app.asar`: the native task
creation handler reads the app selection, while the worktree creator receives
that selection as a request argument and writes the per-worktree Git key.
This is an observation of the installed app, not a public configuration API.

A subsequent user-created session used a detached native worktree at canonical
main `b63c782`. The native creation log recorded `hasLocalEnvironment=true`,
and its Git configuration pointed to the copied VAWS environment file. Setup
selected an immutable dependency environment and generated that revision's
client wiring. A real no-argument MCP call still returned a missing-context
result: this main revision predates the stable user-hook migration and Codex
metadata fix in `8c2f380`. This case establishes native directory creation and
environment execution, while leaving main-based VAWS attachment pending the
feature's integration. No running worktree was moved to hide that distinction.

Read-only inspection also confirmed that the installed app stores the composer
mode by host and project path. After the user's Worktree selection, this
project's saved mode was `worktree`; later tasks from the same project reuse
that choice. This is the app's existing preference, not a VAWS launcher or a
new `config.toml` field. No private application preference was written. A
different project or an explicit Local choice retains its own native behavior.
