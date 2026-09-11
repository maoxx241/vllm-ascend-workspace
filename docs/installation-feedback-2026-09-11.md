Status: 2026-09-11 validation evidence

# Windows installation feedback

The measured bottleneck after downloading was installing many small files.
Using a cache on the environment's filesystem reduced fresh cached installs
from 25.56–26.11 seconds in copy mode to 8.20–8.92 seconds with hardlinks.
The [PowerShell installation guide](windows-installation.md) makes that
configuration and an offline transfer workflow explicit without changing
global cache policy or removing default knowledge capabilities.

## Conditions and measurements

One Windows x64 machine, PowerShell, Python 3.13.12 and uv 0.10.7. Every run
created a fresh environment with `uv sync --locked --group dev`; warm runs
added `--offline --no-python-downloads`. The dependency inputs were workspace
commit `5bac5d8612cc7bb15fdf3e50c31ec9ff88dfd073`, copied into an isolated
source directory. Each environment installed 176 packages.

| Cache state | Link mode | Network policy | Wall time |
|---|---|---|---:|
| Empty dedicated cache | copy | online | 38.37 s |
| Populated cache, first pair | copy | offline | 26.11 s |
| Populated cache, first pair | hardlink | offline | 8.92 s |
| Populated cache, reverse pair | hardlink | offline | 8.20 s |
| Populated cache, reverse pair | copy | offline | 25.56 s |

The cache and environments were on the same local filesystem. Explicit
`--link-mode copy` measured the copy algorithm; this was **not** a direct
cross-drive benchmark. Copy/hardlink order was reversed once to expose a
simple order effect. Two warm samples per mode support the observed range,
not a population percentile or a universal performance promise. The cold
sample includes network/build time and is not a fully cold OS-cache trial.

The dedicated cache contained 56,521 files and 704,731,332 logical bytes
(672.1 MiB). A fresh environment contained 55,645 files and approximately
665.8 MiB of logical data. Hardlinks share file data; adding these logical
sizes does not measure physical disk usage.

## Offline transfer and final dependency verification

The whole dedicated cache was copied to a different directory with native
PowerShell. No symlinks or junctions were found in the original. After its uv
operations completed, `uv cache clean` removed that task's original cache.
The relocated cache then created another fresh environment offline in
8.88 seconds, with no original-cache fallback. The cache copy itself took
33.01 seconds; transfer cost must be counted when preparing a bundle.

After the observation improvements, the final dependency inputs from workspace
commit `c7c7e24f029d05ad91bcc7c75daec55717eab83d` were verified separately:
the two changed owner commits were added to the relocated cache online, then
a fresh offline environment installed all 176 packages in **8.54 seconds**.
This confirms the final lock, rather than only the earlier measurement lock.

Both offline environments passed five checks: imports of remote-dev,
coordinator, knowledge and OpenViking; each of the three package CLI help
commands; and workspace doctor with `outcome: success`. The import check
denied Python socket connect/DNS events and observed none. The child HOME
and runtime-state directories were isolated, and workspace venv re-execution
was disabled so the checks could not silently use the original environment.

This validates cache relocation and environment reconstruction on the same
Windows machine. A second physical machine, Python installation on an empty
OS, different platform, vaws-top deployment, knowledge Release downloads and
NPU service execution were not part of this experiment. Raw measurements,
logs and dependency footprints are retained locally under untracked
`.vaws-local/experience-optimization/`; no endpoint or user identity is needed
in this public report.

## Dependency extras decision

The largest installed distributions, from their recorded files, were:

| Distribution | Files | Logical MiB |
|---|---:|---:|
| volcengine-python-sdk | 26,385 | 190.7 |
| openviking | 1,375 | 81.2 |
| litellm | 2,999 | 53.8 |
| numpy | 949 | 40.1 |
| onnxruntime | 326 | 40.0 |
| babel | 1,122 | 29.5 |
| lark-oapi | 10,734 | 20.3 |

The locked inverted dependency tree places the Volcengine SDK under
`vaws-knowledge -> openviking[ark]`, and lark-oapi under OpenViking. These are
package-owner dependency choices. Moving the entire knowledge package behind
a workspace extra would remove a default capability; it would not selectively
trim those integrations. Keep the complete default dependency set for this
change. Any future minimal package profile belongs with the knowledge owner
and needs separate capability and offline acceptance evidence. The measured
hardlink workflow already improves cached installation without that behavior
change.

The cache/filesystem and offline-option behavior is documented by
[uv cache concepts](https://docs.astral.sh/uv/concepts/cache/) and
[uv CLI options](https://docs.astral.sh/uv/reference/cli/). Measurements above
come from this workspace's local experiment.
