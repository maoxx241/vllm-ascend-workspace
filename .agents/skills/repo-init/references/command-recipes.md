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

Apply the unified alias choice after the machine profile exists:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply-alias --choice machine-username
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply-alias --choice custom --custom-alias team42
python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply-alias --choice none
```

Inspect or maintain only the local identity:

```bash
python3 .agents/scripts/workspace_identity.py summary
python3 .agents/scripts/workspace_identity.py ensure
python3 .agents/scripts/workspace_identity.py set-alias team42
python3 .agents/scripts/workspace_identity.py decline-alias
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

## Resolve CI-pinned vLLM ref

Use this after `vllm-ascend/` is populated and the user chose CI-pinned
alignment:

```bash
python3 .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend
```

Then check out `vllm/` at the returned `vllm_ref`. The resolver prefers
`vllm-ascend/.github/vllm-main-verified.commit`; older checkouts may fall back to a
workflow `vllm_version` or docs `main_vllm_commit` value.

## External dependency plane

The three in-process packages are not submodules. Install them with `python .agents/scripts/vaws_deps.py sync`.
Name capabilities from `doctor`; do not re-derive them.

```bash
python .agents/scripts/vaws_deps.py sync
python3 .agents/scripts/vaws_deps.py doctor
python3 .agents/scripts/vaws_deps.py sync
```

`uv.lock` is the only pin. The packages are public git+https. `uvx vaws-top`
is a separate service. Successful `sync` also prepares the knowledge model and
index through the installed package. Its JSON reports `knowledge.ready` separately
from package installation; pending knowledge does not block ordinary tools. See
[docs/dependency-plane.md](../../../../docs/dependency-plane.md).

Windows PowerShell, using a cache on the workspace filesystem:

```powershell
$cachePath = Join-Path (Get-Location).Path '.vaws-local\uv-cache'
python .agents/scripts/vaws_deps.py sync --locked --group dev --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& .\.vaws-local\venvs\win32\Scripts\python.exe .agents/scripts/vaws_deps.py doctor
```

For offline preparation, exact tool/lock checks and restoring a transferred
cache, use [Windows installation](../../../../docs/windows-installation.md).
Keep the default knowledge package installed; cache placement does not require
changing machine identity, forks or native client settings.

## Quiet main comparison

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo .
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm
python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm-ascend
```

## Remote configuration

Use `configure` only for **explicit fresh setup** after the user selected a topology. Do not use it to migrate established remotes: it unifies fetch and push URLs for the specified `origin` and `upstream`, flattening an explicit `pushurl` or protocol split. Other remotes such as `upstream2` are preserved. For an already-configured clone, choose keep-current and leave fetch/push/protocol/pushurl/extra remotes intact.

Fresh workspace setup example:

```bash
python3 .agents/skills/repo-init/scripts/repo_topology.py configure   --repo .   --origin-url git@github.com:USER/vllm-ascend-workspace.git   --upstream-url git@github.com:vllm-ascend-workspace/vllm-ascend-workspace.git
```

Fresh `vllm-ascend` setup example (community upstream; personal fork is `origin` when the user selected one):

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
# Knowledge setup and background updates

For an explicit knowledge preparation retry or configuration change, run
`python3 .agents/scripts/knowledge_setup.py` (Windows:
`py -3 .agents/scripts/knowledge_setup.py`). Default setup prepares local knowledge
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
