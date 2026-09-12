# Repo-init command recipes

Prefer the helper scripts in `scripts/` and `.agents/scripts/` when possible.

## Probe

Windows, macOS, Linux and WSL use the same entry:

```text
uv run --no-project python .agents/skills/repo-init/scripts/repo_init_probe.py --compact
```

Add `--include-forks` when personal fork discovery is needed. The probe does not
create workspace state or ask setup questions.

## Submodules

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Resolve CI-pinned vLLM ref

Use this after `vllm-ascend/` is populated and the user chose CI-pinned
alignment:

```bash
uv run --no-project python .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend
```

Then check out `vllm/` at the returned `vllm_ref`. The resolver prefers
`vllm-ascend/.github/vllm-main-verified.commit`; older checkouts may fall back to a
workflow `vllm_version` or docs `main_vllm_commit` value.

## External dependency plane

The three in-process packages are not submodules. Install them with `uv run --no-project python .agents/scripts/vaws_deps.py sync`.
Use `doctor` when a capability or dependency pin needs diagnosis.

```bash
uv run --no-project python .agents/scripts/vaws_deps.py sync
```

`uv.lock` is the only pin. The packages are public git+https. `uvx vaws-top`
is a separate service. Successful `sync` also prepares the knowledge model and
index through the installed package. Its JSON reports `knowledge.ready` separately
from package installation; pending knowledge does not block ordinary tools. See
[docs/dependency-plane.md](../../../../docs/dependency-plane.md).

The default per-user uv cache and environment store can be reused by independent
workspaces. To prepare dev dependencies:

```text
uv run --no-project python .agents/scripts/vaws_deps.py sync --locked --group dev
```

For offline preparation, exact tool/lock checks and restoring a transferred
cache, use [Windows installation](../../../../docs/windows-installation.md).
Keep the default knowledge package installed; cache placement does not require
changing forks or native client settings.

## Quiet main comparison

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo .
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm-ascend
```

## Remote configuration

Use `configure` only for **explicit fresh setup** after the user selected a topology. Do not use it to migrate established remotes: it unifies fetch and push URLs for the specified `origin` and `upstream`, flattening an explicit `pushurl` or protocol split. Other remotes such as `upstream2` are preserved. For an already-configured clone, choose keep-current and leave fetch/push/protocol/pushurl/extra remotes intact.

Fresh workspace setup example:

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py configure   --repo .   --origin-url git@github.com:USER/vllm-ascend-workspace.git   --upstream-url git@github.com:vllm-ascend-workspace/vllm-ascend-workspace.git
```

Fresh `vllm-ascend` setup example (community upstream; personal fork is `origin` when the user selected one):

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py configure   --repo vllm-ascend   --origin-url git@github.com:USER/vllm-ascend.git   --upstream-url git@github.com:vllm-project/vllm-ascend.git
```

Optionally set `gh repo set-default` during configure:

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py configure   --repo vllm-ascend   --origin-url git@github.com:USER/vllm-ascend.git   --upstream-url git@github.com:vllm-project/vllm-ascend.git   --gh-default upstream
```

## Branch tracking

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py ensure-main   --repo vllm-ascend   --remote origin
```
## Knowledge setup and background updates

For an explicit knowledge preparation retry or configuration change, run
`uv run --no-project python .agents/scripts/knowledge_setup.py`. Default setup prepares local knowledge
and shared downloads, preserving existing publishing choices. Add `--contribute`
only to enable authorized public contribution; `--read-only` disables contribution
while keeping shared downloads. `--repository OWNER/REPO` changes the shared
corpus without enabling contribution. Only contribution setup needs a GitHub
login and fork; no token belongs in tracked files.
Refresh the selected clients with `vaws_client_setup.py --apply` afterward.
The package MCP service maintains the model, index and shared releases while
alive. Windows and WSL use the Windows knowledge owner for the same mounted
workspace; a missing Windows interpreter is reported as pending. Independent
Linux workspaces use their own environment. Knowledge PR review and merge remain
manual. Ordinary development requires no maintenance commands.

## Start an isolated native client

```text
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

The default creates an independent Git copy before the first native tool runs.
An explicit existing workspace is reused. Setup pins prepared environments and
preserves the native client's session identity. See the
[platform contract](../../../../docs/platform-contract.md).
