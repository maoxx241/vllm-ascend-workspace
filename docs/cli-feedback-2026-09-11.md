# CLI feedback measurements

Status: dated 2026-09-11

The workspace launcher now defers task-state discovery until an operation needs
it and performs one package check per launch. Explicit coordinator help bypasses
workspace environment discovery. remote-dev imports execution modules only after
parsing and basic argument validation.

## Windows paired comparison

Python 3.13.12 on Windows; 20 fresh processes per revision and case, alternating
baseline/candidate order. Both revisions ran from source worktrees with the same
installed dependencies. Baselines: workspace `ecd45ba`, remote-dev `34a460d`.
Times include interpreter startup and output capture. These are local feedback
measurements, not remote execution or NPU performance measurements.

| Command | Median before / after (ms) | p95 before / after (ms) | Median reduction |
| --- | ---: | ---: | ---: |
| Workspace help | 190 / 132 | 205 / 140 | 30% |
| Workspace session help | 424 / 234 | 432 / 238 | 45% |
| Workspace invalid option | 190 / 131 | 205 / 137 | 31% |
| remote-dev help | 189 / 161 | 203 / 173 | 15% |
| remote-dev read help | 178 / 152 | 182 / 162 | 15% |
| remote-dev invalid option | 191 / 163 | 194 / 173 | 15% |
| remote-dev missing endpoint | 177 / 151 | 185 / 158 | 15% |

One additional process per revision used an empty Python bytecode cache. The
workspace session-help sample changed from 743 to 552 ms; other candidate samples
were 373-472 ms. This is not an OS-cold benchmark: the OS file cache was not reset.
Single first-run samples do not establish a latency distribution.

Exit codes and help/error output matched. For structured missing-endpoint errors,
semantic fields matched after excluding generated timestamps and result IDs.
Initial measurements of coordinator and knowledge help were already below one
second; those packages received no speculative startup rewrite.

## Validation

- Workspace parser, coordinator consumer and scaffold safety tests: 47 passed,
  including 33 subtests.
- remote-dev parser and client parity tests: 46 passed, 13 platform skips,
  including 41 subtests.
- Fresh-process guards reject execution-module imports, socket connections,
  subprocess creation and shell execution during help. An isolated home remains
  empty. remote-dev covers every tool's help; workspace covers root, status,
  environment, session and run help plus a root argument error.

Raw samples and JUnit receipts are retained in untracked local test output.
Hosted CI remains the authority for Linux/macOS behavior on the submitted code.
