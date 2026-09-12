# Local test progress and retries

Status: current

Run the workspace's local Python tests with visible progress and retained evidence:

```powershell
python .agents/scripts/vaws_deps.py sync --locked --group dev
uv run python .agents/scripts/local_tests.py --jobs 2 --timeout 600
```

The default selection runs each workspace test file separately and each skill's
test directory as one suite. Use `--split file` to isolate every file, or provide
repository-relative files/directories to narrow the selection. These commands
run local control-plane tests; device execution remains in managed remote runs.

```powershell
uv run python .agents/scripts/local_tests.py .agents/tests/test_local_tests.py --heartbeat 5
uv run python .agents/scripts/local_tests.py .agents/tests --pytest-arg=-x
uv run python .agents/scripts/local_tests.py --rerun-failed .vaws-local/test-runs/<run>/summary.json
```

The runner prints start, periodic running and completion lines to stderr with the
current file/suite, elapsed time and log path. Each subprocess has its own log and
JUnit file. One JSON summary goes to stdout and is atomically updated under
untracked `.vaws-local/test-runs/` while the run progresses. Redirect stdout to
a file if a console should show only progress. Default concurrency is one and
the default per-subprocess timeout is 600 seconds; neither failure nor timeout
prevents unrelated pending cases from running.

Each row retains the original pytest exit code, JUnit counts and one of `passed`,
`failed`, `timed_out`, `interrupted`, `error`, or `not_run`; active rows temporarily
show `running`. A zero exit without a valid, nonempty JUnit report is an error.
The runner exits 0 only when every case passed, 1 on test failure, 2 on an input
or setup error, and 128 plus the signal number on a handled interruption.

Each invocation runs the selected cases against the current code. It does not
scan business source submodules or installed dependencies before starting, and
it does not cache successful test results. Use a narrow selection for iteration;
previous logs remain evidence of the code that was tested at that time.

`--rerun-failed` selects only unsuccessful or unfinished cases from a previous
summary and runs them again. Its new summary describes those cases alone; it does
not report earlier passing cases as newly verified. The original pytest arguments
are retained unless `--pytest-arg` replaces them. If no cases need rerunning, the
command exits 0 with `status: nothing_to_rerun` and does not launch pytest. Summary
schema v2 stores this selection without whole-workspace fingerprints.

Windows child trees belong to a job object created before the interpreter is
resumed. POSIX children use a dedicated process group. Completion, failure,
timeout and handled interruption drain owned descendants, including processes
left by a crashed pytest child. Windows also drains on abrupt runner termination.
POSIX hard-kill of the runner itself and deliberately escaped sessions are outside
this local runner's cleanup guarantee. Logs and partial summaries remain for
inspection. This runner does not create resource leases or execution ledgers.
