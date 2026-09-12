# Agent-only workflow acceptance

Status: 2026-09-11 validation evidence

This change applies the [design principles](design-principles.md)
to workspace entries, business reports and their runtime owner. The workspace
delivery is [PR #143](https://github.com/vllm-ascend-workspace/vllm-ascend-workspace/pull/143);
the owner dependency is merged [coordinator PR #15](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/15).

## Delivered behavior

| Before | After |
| --- | --- |
| Duplicate remote and knowledge wrappers; separate serving scripts | Installed owner APIs and one serving entry; retired interfaces are removed |
| Agents create report IDs and advance plan/record/finalize steps | One business call consumes cases and results and generates the report and manifest |
| Agents assemble A/B execution and runtime metadata | The collector binds actual worktrees, alternates runs, warms services and cleans up through coordinator |
| Service lookup and launch facts reconstructed by consumers | Coordinator resolves task-owned services and exposes persisted launch observations |
| Legacy project knowledge schemas and incident instructions | 22 package-prepared Markdown notes preserve conditions, missing provenance and historical uncertainty |
| A shared virtual environment and terminal-dependent text handling | Separate platform environments, preserved Python flags/module arguments and consistent Git text normalization |

The current inventory changed from 97 to 69 entries, supported entries from 51
to 39, and compatibility entries from 17 to zero. The 20 business/setup skills
remain discoverable, with updated references, examples, metadata and client
projections. Counts describe the implementation; they are not an API design quota.

## Platform and execution evidence

Windows PowerShell 5.1.26100.9444 and PowerShell 7.6.5 were exercised on native
Windows. WSL used Ubuntu 24.04 in the same NTFS checkout. Both platform
environments installed the lock successfully and ran Python 3.13.12.
The previous virtual environment was preserved.

- System-Python startup and doctor succeeded in both PowerShell versions and
  WSL. Module entry, interpreter flags, Unicode and space-containing arguments
  survived the platform interpreter hop.
- Actual generated hooks were executed under PowerShell 5.1, PowerShell 7 and
  cmd. Arguments, stdin and exit codes survived literal special characters.
- The same one-call operator-report input was invoked from both PowerShell
  versions and WSL using a Unicode input path. With no case results supplied,
  all three generated an `inconclusive` manifest and named both missing cases.
  This checks report execution with synthetic inputs, not an NPU operator run.
- Windows and WSL Git produced identical filtered object IDs for the checked
  documentation and Python source. A regression case also checks LF/CRLF under
  all three `core.autocrlf` settings against the repository attributes.

## Tests and corrections

| Check | Observed result |
| --- | --- |
| Native Windows workspace selection | All 57 test groups passed in CI: 1,580 JUnit entries, including subtests, with 3 skips; the subsequently added Git normalization case passed in the 3-case platform suite |
| WSL workspace selection | 56 groups completed with 1,566 JUnit entries, zero assertion failures and 10 skips; the remaining 12-case group passed on a separate rerun; the added Git normalization case also passed |
| Coordinator owner, Windows | 194 passed, 5 skipped, 42 subtests passed |
| Coordinator owner, WSL | 198 passed, 1 skipped, 42 subtests passed |
| Skill validation and projections | All 20 skills passed; generated client projections matched |
| Inventory, boundaries, tracked paths and leak checks | Passed; no new boundary/path violations or unsuppressed leak findings |

The first Windows run exposed an obsolete benchmark preset expectation. CI
then found two stale test assumptions: a mandatory knowledge section heading
and historical path evidence being treated as executable configuration. These
were corrected and the affected checks passed. The Linux CI recipe also had
a redundant `uv pip` invocation targeting an old environment; it now consumes
the already locked dev dependencies.

WSL exposed different Git newline interpretation between terminals. Repository
attributes now normalize text consistently. During the initial dirty-tree run,
repeated snapshots of the workspace and populated submodules on NTFS made one
group exceed its 900-second limit. Its isolated rerun passed all 12 cases in
132.51 seconds. The initial timeout remains recorded; it is not counted as an
initial full-run pass. Large shared-checkout scans remain filesystem-sensitive.

Raw local stdout, stderr, JUnit files, install receipts and shell checks are
retained under untracked `.vaws-local/agent-flow-review/` and
`.vaws-local/test-runs/`. Public CI records are attached to the linked PRs.
The coordinator pin is `90b047794465346eef000640e44c0bb2cbfb5bb5`;
`pyproject.toml` and `uv.lock` record the complete dependency selection.

## Limits

This is local control-plane, consumer and shell acceptance. No remote NPU
model workload, throughput result, PD KV-transfer proof or new macOS acceptance
is claimed. Runtime tests use controlled fixtures where hardware would be
required. Missing observations and incomplete case coverage remain unknown or
inconclusive; passing a supplied report does not establish a model rerun or a
root cause. Historical knowledge was migrated without revalidating its model
claims, and private candidate storage was preserved.
