# Native platform contract

Status: current

Windows, macOS and Linux use the same task concepts, argument names and result
schemas. Platform differences belong to the implementation of paths, process
ownership and dependency preparation. They do not require separate Agent
workflows for PowerShell, WSL, bash or zsh.

## Entry points

The portable bootstrap prefix is `uv run --no-project python`. It avoids relying
on whether a machine names its Python command `python` or `python3`, and does not
ask the user's shell to activate a virtual environment.

```text
uv run --no-project python .agents/scripts/vaws_deps.py sync --group dev
uv run --no-project python .agents/scripts/vaws_deps.py status
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

The client entry uses an installed native CLI. It creates an independent editing
directory by default and reuses an explicitly supplied existing directory.
Client-specific arguments, including native resume IDs, follow `--`. The actual
process cwd is set before its first tool call. Native desktop clients continue
to own their UI's Local/Worktree choice; a session hook cannot move a running
parent application.

The child CLI receives the selected native environment through PATH and
VIRTUAL_ENV. Its bare `python` and `uv run --no-project python` use that environment;
no shell activation or global environment edit is required. Dependency updates
prepare another environment through sync rather than changing a published one.

## Commands and files

Internal commands pass an argument vector, explicit cwd and environment to the
process API. Spaces, Unicode, quotes and shell punctuation remain data. File
copying, directory creation and local state use filesystem APIs. No user shell
activation, `cd && ...`, or PowerShell-to-bash string translation is needed.

An explicitly supplied shell script still has a declared interpreter. Remote
Linux shell commands use the remote bash contract on every client platform,
including the same runtime initialization and command scope. Arbitrary shell
programs cannot be translated between PowerShell and bash without changing
their meaning.

Owned attached local processes use Windows Job Objects or POSIX process groups.
Timeout and close include their still-owned descendants even when the original
parent has already exited. Unrelated processes remain outside this scope.
Cleaning up a local SSH process does not by itself prove a remote execution has
stopped; managed execution status remains with its execution owner.

## Paths and environments

Native paths are converted at the boundary to the process that will read them.
A shared Windows-mounted WSL workspace has one Windows coordinator and knowledge
owner. It must use a verified Windows environment, not a path constructed using
Linux's Python ABI. Independent native editing copies have ordinary `.git`
directories, avoiding absolute linked-worktree pointers from the other OS.
Empty uninitialized submodules remain uninitialized; source is not fetched merely
to start a workspace editing task. Initialized submodules keep independent state.

Prepared dependency environments have content identities including lock inputs,
effective dependency selection and the actual Python platform/ABI. Published
environments are immutable. Updating dependencies prepares another environment;
it does not upgrade the interpreter of an already running client or daemon.
Generated hooks and MCP entries pin their selected environment. The native client
also pins its managed owner environment independently, so a later lock edit in a
WSL shell cannot switch the owner used by an already running task. Project-relative
launch aliases are also per identity and never redirected to another version.

Setup and normal startup have different costs: explicit sync prepares missing
dependencies, while startup only resolves a completed environment. Knowledge
preparation remains an optional capability after sync and does not invalidate a
completed package environment when model/index preparation is unavailable.

## Verification boundaries

Native Windows and macOS CI exercise the same behavior tests on Python 3.13.
Linux CI covers the declared minimum Python 3.11. WSL is supplementary evidence
for Windows/Linux interoperability; it is not a substitute for macOS execution.
Behavior cases cover literal arguments, UTF-8, cwd, process lifetime, independent
indexes and fixed environment selection. Duplicate help-string checks and
repeated whole-repository scans are not substitutes for those behaviors.
