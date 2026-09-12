Status: current

# Windows installation and offline transfer

These are Agent execution recipes for PowerShell in the workspace root. Prepare Windows x64
Python, uv and Git first. The validated combination is Python 3.13.12 and uv
0.10.7; the project supports other Python versions, but a prepared cache must
be validated again for another platform, interpreter or uv version. See the
[installation measurements](installation-feedback-2026-09-11.md).

## Online installation with a reusable user cache

```powershell
$workspaceRoot = (Get-Location).Path
$cachePath = Join-Path $env:LOCALAPPDATA 'vaws\offline-preparation-cache'
uv run --no-project python .agents/scripts/vaws_deps.py sync --locked --group dev --python 3.13 --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
uv run --no-project python .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

`--cache-dir` applies to this command. Keep using the same cache path on later
syncs. The default environment store is under local application data, outside the
checkout. A cache on the same filesystem permits hardlinks; cross-filesystem
installations fall back to copying. The ready receipt records the physical store
path, including MSIX application-data redirection where present. A junction or
mounted directory can still cross filesystems despite sharing a drive letter.
Use `--link-mode copy` when the destination filesystem cannot hardlink.
No global uv settings need to change. See
[uv cache location](https://docs.astral.sh/uv/concepts/cache/#cache-directory)
and [uv sync options](https://docs.astral.sh/uv/reference/cli/#uv-sync).

`--group dev` includes the local test dependencies. Omitting it does not remove
the three required runtime packages or the default knowledge capability.
The environment builder resolves the selected Python to its physical versioned
path. Install that Python before preparing an offline bundle. It accepts only
options represented in the immutable build contract; unknown options fail.

## Prepare an offline bundle while online

First complete the online sync above for the exact checkout and target Python.
Wait for all uv operations using this cache to finish before copying it.
Create a new bundle directory; copy the entire cache without changing its
internal files. The manifest records the lock and tool combination.

```powershell
$bundlePath = Join-Path $workspaceRoot ('.vaws-local\offline-bundle-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $bundlePath) { throw 'Choose a new bundle directory' }
New-Item -ItemType Directory -Path $bundlePath -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $cachePath -Destination (Join-Path $bundlePath 'uv-cache') -Recurse -Force -ErrorAction Stop
$receipt = uv run --no-project python -c 'import sys,json; from pathlib import Path; sys.path.insert(0,".agents/lib"); from vaws_environment import native_ready; print(json.dumps(native_ready(Path.cwd())))' | ConvertFrom-Json
$pythonIdentity = & $receipt.python -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Python identity' }
$manifest = [ordered]@{
    uv = (uv --version)
    python = $pythonIdentity
    pyproject_sha256 = (Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash
    lock_sha256 = (Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Encoding utf8 -ErrorAction Stop
$bundlePath
```

Transfer that bundle and the matching workspace checkout separately. Prepare
the Python/uv/Git installers while online if the destination lacks them. Do not
copy a prepared virtual environment as the installation: recreate it from the lock and cache. The
bundle does not include model weights, remote containers, shared knowledge
Release downloads or the separate vaws-top service. Those have their own
preparation and storage requirements. Native client configuration belongs to
the destination machine; see [repo-init](../.agents/skills/repo-init/SKILL.md).

## Recreate the environment offline

Run from the matching destination checkout with the bundle path resolved from
the transfer operation. The following checks prevent accidentally
using a bundle prepared for a different lock or interpreter. Use a new local
cache directory instead of merging files into an active uv cache.

```powershell
$workspaceRoot = (Get-Location).Path
# $bundlePath is the actual transferred bundle directory selected by the Agent.
$manifest = Get-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Raw -ErrorAction Stop | ConvertFrom-Json
if ((uv --version) -ne $manifest.uv) { throw 'Install the uv version recorded in manifest.json' }
$preparedPython = uv python find --offline 3.13
if ($LASTEXITCODE -ne 0) { throw 'Install the prepared Python interpreter first' }
$pythonIdentity = & $preparedPython -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Install the prepared Python interpreter first' }
if ($pythonIdentity -ne $manifest.python) { throw 'Python version or platform differs from the prepared cache' }
if ((Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash -ne $manifest.pyproject_sha256) { throw 'pyproject.toml differs from the bundle' }
if ((Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash -ne $manifest.lock_sha256) { throw 'uv.lock differs from the bundle' }
$cachePath = Join-Path $env:LOCALAPPDATA ('vaws\offline-cache-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $cachePath) { throw 'Choose a new cache directory' }
New-Item -ItemType Directory -Path (Split-Path -Parent $cachePath) -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath (Join-Path $bundlePath 'uv-cache') -Destination $cachePath -Recurse -Force -ErrorAction Stop
uv run --offline --no-project --python $preparedPython python .agents/scripts/vaws_deps.py sync --locked --group dev --python $preparedPython --offline --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Offline sync failed; retain the output and prepare the missing cache entries online' }
uv run --no-project python .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

The destination must have the same Python build available to uv; an explicit
path to that interpreter can replace `uv python find`. `--offline` limits uv to local and
cached data. A failure means the bundle, interpreter or selected dependencies
are incomplete; prepare those on a connected machine with the same lock and
retry. A changed lock needs a new prepared cache. Preserve the failed output.

Inspect the doctor's JSON `outcome` and individual capabilities. Successful
package installation does not prove remote access or NPU execution. A missing
optional fleet monitor may appear separately from the installed packages.
Use `uv cache clean --cache-dir $cachePath` only when intentionally discarding
that cache; do not manually alter uv's internal cache layout.
