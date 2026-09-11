# Windows non-NPU validation

Status: dated 2026-09-11 — completed within the scope and limits below

## Scope and evidence

Native Windows 11 x64, PowerShell 7 and Windows PowerShell 5.1, CPython 3.13.
Starting workspace: `bae3b80`. The pass covers local clients, hooks, scripts,
offline skill logic, three runtime owners, and ordinary SSH operations against
four explicitly supplied Linux servers. Tests use isolated state, synthetic
topology and small source/file fixtures. No NPU allocation, device execution,
model serving or performance benchmark was performed.

Detailed logs and JUnit XML remain in untracked
`.vaws-local/windows-validation/`. Private endpoint details stay there and
must not be published. This report contains only scope, results and repairs.

| Area | Evidence |
| --- | --- |
| Locked installation | All three repair commits published, locked and installed through `uv sync --locked --group dev`; doctor passed; no editable package overrides remain |
| Workspace client contracts | After locked reinstall: 517 passed, 31 skipped, 334 subtests passed; `workspace-locked-complete.xml` |
| Every skill's local tests | All 20 suites passed; 796 JUnit cases including subtests, 1 skip, 0 failures/errors; `skills-results.json` and `skills-*.xml` |
| remote-dev native Windows | 314 passed, 80 skipped, 125 subtests passed; `remote-native-complete.xml` |
| remote-dev Linux regression | 380 passed, 2 skipped, 153 subtests passed under WSL; `remote-linux-complete.xml` |
| Coordinator native Windows | 179 passed, 5 skipped, 42 subtests passed; `coordinator-native-complete.xml` |
| Coordinator affected Linux contracts | 35 passed, 8 subtests passed; one venv case excluded because this WSL Python lacks ensurepip; `coordinator-linux-affected.xml` |
| Knowledge native Windows | 713 passed, 5 skipped, 58 subtests passed; `knowledge-native-complete.xml`. Three Git-output decoding warnings found here were fixed; 19 affected tests then passed with thread warnings treated as errors in `knowledge-contribution-unicode.xml` |
| Dense knowledge distribution | Explicit existing CPU model cache: real OpenViking build, release, import, version switch, modify/delete visibility, private-layer preservation and restart passed; `knowledge-native-chain.xml` |
| Official MCP stdio | Real SDK initialize/list/call and coordinator attach/resume/bind/run-unavailable/finish passed; remote read/write preserved Chinese text and emoji |
| Hooks and interpreter bootstrap | Real cmd, Windows PowerShell and pwsh preserved literal special arguments, Unicode pipes and nonzero exit; abrupt parent exit stopped child trees; normal CLI exit preserved intentional detached services |
| Fleet monitor | Released uvx package deployed, started, reported healthy, restarted with a new PID, stopped and released its HTTP port; empty inventory/host pool avoided hardware polling |
| ModelScope lifecycle | Offline SDK/API fixtures verified partial-download resume, durable background worker, repeated status without killing it, Unicode paths, SHA256 success and same-size corruption failure |
| Repository checks | Boundary/path enforcement, skill catalog, generated projections, lock consistency and staged leak scan passed with zero leak findings; final affected guard tests: 93 passed, 57 subtests passed |

### Live SSH and source publication

Each of the four servers passed 30 ordinary tool calls: command execution,
Unicode/LF write/read/edit/multi-edit/append, ls/glob/grep, patch add/update/delete,
rejected root escape, stderr/nonzero exit, timeout, artifact byte hashes,
interactive job stdin/EOF/tail, and job stop/status. These 120 calls used separate
temporary directories.

Additional tests covered streaming output on all four servers; SDK MCP calls;
a loopback HTTP server accessed through an SSH forward; close and port release;
streaming timeout followed by remote PID disappearance; and multi-level Chinese
artifact paths carrying all byte values. Source-only publication passed initial
Git push, incremental Git push and Git bundle transfer. Dirty and untracked
contents from two Chinese local worktree paths matched the remote snapshot
commits. Local worktrees remained unchanged, and remote test directories and
owned jobs/forwards were cleaned.

Two initial read/status calls timed out. Isolated retries passed. The cause of
the intermittent transport delay remains unestablished; this is an observation,
not an attribution to DNS, a server or the client.

## Confirmed defects and repairs

1. **Windows status checks terminated processes.** Monitor and ModelScope used
   `os.kill(pid, 0)`. They now observe typed Win32 process handles. Background
   launch is hidden, termination handles process trees, and parent log handles
   close promptly.
2. **Interpreter replacement broke MCP lifetime and exit propagation.** Windows
   `execve` spawned a replacement beyond the parent handle. The dependency
   bootstrap now waits in a job object; coordinator module dispatch stays in
   process. Tests cover stdin, nonzero exit, abrupt termination and service survival.
3. **Shell hooks broke on spaces, quotes and Unicode.** Generated Windows hooks
   use literal PowerShell encoded commands with UTF-8 pipes. Ownership detection
   recognizes only the exact generated launcher. Git hook shims use quoted POSIX
   paths and LF bytes for Git's shell.
4. **Linux scripts acquired CRLF in Windows stdin pipes.** remote-dev now sends
   UTF-8 script bytes unchanged. This fixes real bash option/heredoc failures.
5. **Nested remote paths used Windows separators.** Artifact manifest keys and
   coordinator mirror/bundle/manifest/runtime paths now use POSIX semantics,
   including upload parent directories. Remote PATH uses `:` independently of
   the client OS.
6. **Unicode output depended on the Windows code page.** CLI protocols, local Git
   subprocesses, configuration reads and diagnostics specify UTF-8. Real Chinese
   worktree snapshots and contribution Git output were exercised. PR validation
   on an English Windows runner additionally exposed leak-scanner, Git-hook and
   skill-catalog output failures. Shared CLI bootstrap now initializes UTF-8
   output, including when the interpreter hop is skipped. Native tests preserve
   Chinese paths and emoji with either cp1252 or cp936 in the starting environment.
7. **Coordinator assumed Unix sockets and flock.** Windows uses a byte-range lock
   and token-authenticated IPv4 loopback IPC. Startup is serialized; marker
   publication is atomic; stale markers recover; startup failures show daemon
   errors. Parallel start, authentication, stop and restart use real processes.
8. **Knowledge shutdown leaked processes and locked files.** Win32 declarations
   preserve 64-bit handles; waitpid remains POSIX-only; Windows exit is observed
   after tree termination. Failed shutdown preserves the ownership record.
   Parent embedding/OpenViking log handles no longer keep files locked.
9. **Knowledge paths and commands assumed one POSIX drive/shell.** Cross-drive
   relative-path fallback and safe snapshot components preserve containment.
   Conformance accepts JSON argv as well as host-shell strings; Windows timeouts
   stop process trees. ZIP inspection checks original member names before Windows
   normalization can hide malformed names.
10. **Skill helper imports collided.** Serving and benchmark helpers now have
    distinct module names; callers, tests and generated projections were updated.

Linux worker fixtures requiring fcntl, FIFOs, executable modes or a Linux shell
skip on native Windows. Actual Linux payloads remain covered by WSL and live SSH.
Portable tests use byte-exact files, real Git repositories, owned temporary
state and explicit cleanup. They no longer accept a crashed help process or
compare unrelated concurrent temporary directories.

## Delivery

Owner fixes are published on each repository's `codex/windows-validation`
branch and pinned by full commit in `pyproject.toml` and `uv.lock`:

| Owner | Commit |
| --- | --- |
| remote-dev | [9120004](https://github.com/vllm-ascend-workspace/remote-dev/commit/9120004fd30967c38b35965af7d2b0cbae9a6809) |
| vaws-coordinator | [a962d08](https://github.com/vllm-ascend-workspace/vaws-coordinator/commit/a962d080382d0b6617c945ea038af6c6fe361f20) |
| vaws-knowledge | [89c273b](https://github.com/vllm-ascend-workspace/vaws-knowledge/commit/89c273b6f3bf184614ec2463d40f91e8f048d45b) |

Native Windows CI was added to the workspace, coordinator and knowledge;
remote-dev's portability job now runs the complete client suite. These jobs are
authored and locally exercised. The table above describes the local validation
cutoff before PR submission. Hosted CI and landing status are recorded in
[the cross-repository tracking issue](https://github.com/vllm-ascend-workspace/.github/issues/5).
Runtime-owner PRs must land before the workspace consumer PR; commit pins keep
the tested code identifiable throughout that sequence. CI follow-ups select a
working Git Bash for local Linux-peer fixtures and keep POSIX-only grep fixtures
on Linux/macOS while preserving native Windows search coverage.

## Experience and efficiency notes for discussion

These are proposals for later discussion, not new execution gates.

| Observation | Suggested follow-up |
| --- | --- |
| Local configuration errors sometimes appear only after heavy imports | Validate inexpensive arguments/configuration before importing large dependency trees |
| Knowledge install includes many parsers and CPU inference packages | Document download/cache size and an offline package-cache workflow; discuss a supported minimal client extra separately |
| Cross-drive uv cache cannot hardlink | Document `UV_LINK_MODE=copy` for that layout |
| Repeated Git identity snapshots dominated schema property tests | Keep real Git integration cases; use fixed identity in schema-only tests; this repair already reduced the affected group from timeout to seconds |
| A liveness check terminated the monolithic test runner | Retain bounded per-suite logs/XML and aggregate results for resumable diagnosis |
| Examples often assume POSIX paths, quoting and executable bits | Add Windows invocation examples beside the owning interface |
| Native Windows OpenSSH lacks supported ControlMaster | Preserve independent connections; measure batching before changing transport semantics |
| Two read/status calls timed out before retries passed | Add connection/transport phase timing before choosing retry and timeout policy |
| Services survived failed fixture cleanup | Register owned cleanup immediately; retain ownership records until exit is confirmed |
| Help-only checks accepted a crashed Windows process | Keep exit-code and real stdin/lifetime assertions in native CI |
| Long checks can produce little progress | Prefer per-file progress, persistent logs and bounded waits; reuse unaffected evidence |

### Proposed implementation order

This is a proposal for follow-up work, not a claim of implemented behavior.
Process cleanup, help-process exit checks and redundant Git snapshots in
schema-only property tests were already repaired in this validation.

| Order | Concrete change and owner | Acceptance evidence |
| --- | --- | --- |
| 1 — faster local feedback | Workspace and package CLI owners: measure cold/warm help, malformed arguments and missing configuration; parse these before loading heavy backends. Keep real operations on the existing package entry points. | Same error/exit contract and Unicode behavior; no network or service startup for help/local argument errors; compare median and p95 before/after. Target common help/local errors below one second on the validation machine. |
| 1 — repeatable local checks | Workspace: promote the bounded per-suite validation runner into a maintained helper using ordinary pytest/JUnit. Emit suite, elapsed time and log path to stderr; keep one summary on stdout. Allow bounded concurrency and failed-only reruns with the exact source/dependency/platform fingerprint. | Inject a crash, timeout and interruption: unrelated suites finish, exit code remains nonzero, logs survive, owned children exit. Changed inputs invalidate previous pass results. Keep Windows and Linux coverage explicit. |
| 2 — explain slow operations | remote-dev/coordinator owners: extend existing runtime-feedback fields with measurable phase timing. Separate local startup, SSH connection, remote execution and transfer where observable; report unknown where SSH cannot distinguish phases. Reuse existing progress/record references. | Controlled slow connect/command/transfer cases produce distinct evidence; steady-state status stays quick; no credential leakage, global SSH changes or replay of arbitrary business commands. |
| 2 — cheaper repeated SSH reads | remote-dev: first benchmark independent Windows connections and existing calls. Consider one explicit bounded batch for related read-only observations only if round-trip savings are material. | Same endpoint/path policy, per-operation outcomes and bounded output; compare p50/p95 and round-trip count. Mutating commands retain their current one-shot semantics. |
| 3 — installation and Windows recipes | Workspace/knowledge owners: document measured package/cache size, initial and cached install times, offline wheel/cache preparation, PowerShell invocation and cross-drive copy mode. Evaluate a client/backend dependency split only after measuring its benefit. | A clean Windows environment can install from the prepared offline inputs and run required capabilities. Cached reinstall downloads no unchanged wheels. Existing full knowledge behavior remains the workspace default; any optional package split needs its own compatibility test. |

Each follow-up should be a small owner PR plus a consumer change only when
needed. Report baseline, changed behavior and measured improvement in the PR.
Reuse the existing runtime-feedback contract; do not add another scheduler,
resource ledger or recovery workflow to the workspace.

## Limits

Real hosted ModelScope download/authentication and large weight transfers were
not exercised; lifecycle and integrity used an offline SDK fixture. External
review providers, real contribution PRs and public knowledge release publication
were not invoked; local contracts used fake services. Client configuration was
tested in isolated files and real shells, rather than changing every installed
application's user settings. macOS and Windows ARM64 were outside this pass.
NPU execution requires a separate validation on managed Ascend hardware.
