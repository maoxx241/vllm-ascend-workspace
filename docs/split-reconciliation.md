# Split reconciliation

`vllm-ascend-workspace` is being split into several repositories under the
`vllm-ascend-workspace` organization. Each extraction wrote a careful
`docs/HANDOFF.md` saying what it removed and where that thing now lives. The
handoffs were accurate. Nobody compared them against the destinations, and
three items fell into the gap between two repositories, each side believing
the other had them.

This document describes the mechanism that closes that gap: a tracked
**split ledger** that records every declared move, and a **checker** that
establishes, per row, whether the destination actually has the item. It sits
next to the boundary guard (`docs/repo-boundaries.md`) and follows its
conventions: policy as tracked data, attributed rows, and a hard failure when a
row stops matching reality so a fix and its ledger update land together.

The boundary guard answers "does the scaffold still reach into something that
is leaving?". The ledger answers "did the thing that left arrive anywhere?".

| Piece | Path |
|-------|------|
| Ledger | [`.agents/policy/split-ledger.json`](../.agents/policy/split-ledger.json) |
| Checker | [`.agents/scripts/split_reconcile.py`](../.agents/scripts/split_reconcile.py) |
| Tests | `.agents/tests/test_split_reconcile_ledger.py` |
| CI | `.github/workflows/split-reconcile.yml` |

---

## 1. Declared and arrived are two facts

Every ledger row carries both, separately attributed:

```json
{
  "id": "remote-dev.managed-jobs-supervisor",
  "what": "Linux remote job receipt protocol: the child-subreaper supervisor ...",
  "kind": "module",
  "source":      {"repo": "remote-dev", "path": "core/managed_jobs.py",
                  "scaffold_path": ".remote-dev/core/managed_jobs.py"},
  "destination": {"repo": "vaws-coordinator", "path": "...",
                  "evidence": [{"kind": "symbols", "glob": "*managed_jobs.py",
                                "exclude": ["tests/*", "docs/*"],
                                "names": ["control_job", "job_status", "worker"]}]},
  "declared_by": [{"repo": "remote-dev", "commit": "900ad15...",
                   "document": "docs/HANDOFF.md", "section": "4.2",
                   "says": "The supervisor moves to vllm-ascend-workspace/vaws-coordinator"}],
  "recorded":    {"state": "missing", "observed_on": "2026-09-07",
                  "observed_commit": "f7c0682...", "observed_by": "...",
                  "follow_up": "vaws-coordinator pull request #1 ..."}
}
```

* `declared_by` is what somebody *said*: the commit or handoff section, quoted.
  A row with no declaration is rejected; the ledger is not a wish list.
* `destination.evidence` is what would have to be true in the destination for
  the item to count as arrived. A row with no destination repository, or with
  no evidence, is rejected: an arrival nobody can check is not a destination.
* `recorded` is what a named observer *saw* at a specific destination commit.
  `arrived` and `missing` both require `observed_commit`; `missing` requires a
  `follow_up` naming who or what closes the gap; `unverified` requires `why`.

The checker re-derives the observed state and compares it with `recorded`.
Any disagreement with a *definite* observation fails `--mode enforce`:

| recorded | observed | result |
|----------|----------|--------|
| missing | arrived | **fail** `arrived-not-recorded`: flip the row, cite the commit |
| arrived | missing | **fail** `regression` |
| unverified | arrived or missing | **fail** `recorded-unverified-now-observed` |
| anything | unverified | pass; the row is reported `unverified` |

There is deliberately no flag that rewrites the ledger from observations. A
row flips because a person or agent looked and wrote down what they saw.

### Evidence kinds

| kind | proves |
|------|--------|
| `path` | the file or directory exists |
| `symbols` | one Python file (at `path`, or matching `glob` minus `exclude`) *defines* every listed name, found by AST, so a comment or string does not count |
| `sha256` | the file is byte-identical to a pinned digest (vendored copies) |
| `text` | the file contains a literal marker |
| `commit` | the commit is an ancestor of the destination's `HEAD` (history moves) |
| `reference` | some file matching `glob` minus `exclude` matches a regex (a registration or call site exists somewhere) |

Globs are `fnmatch` patterns over checkout-relative paths; `*` crosses `/`.
`scan.skip_roots` (the two submodules, `node_modules`, untracked state) are
never searched.

---

## 2. Destination verification: what `arrived` means

Two of the four destinations (`remote-dev`, `vaws-top`) are private. The
scaffold is public, so its CI cannot read them and must not hold credentials
that could. The checker therefore never touches the network. It inspects
**local checkouts** handed to it:

```bash
python3 .agents/scripts/split_reconcile.py \
  --destination vaws-coordinator=/path/to/vaws-coordinator \
  --destination vaws-top=/path/to/vaws-top
```

(or `VAWS_SPLIT_DESTINATIONS="name=path:name=path"`). For each checkout it
records the `HEAD` commit and refuses a checkout whose `origin` belongs to a
different repository. A destination with no checkout is `unverified`.

So, in each context:

| Where | Can read | `arrived` rows for private destinations |
|-------|----------|------------------------------------------|
| Public CI (`split-reconcile.yml`) | scaffold + the public destinations, checked out without credentials | reported `unverified`; the recorded state and its attribution are shown, not confirmed |
| A maintainer with org access, locally | everything | confirmed or contradicted at the commit they cloned |

**What an `arrived` verdict establishes:** the declared evidence was found in
a checkout whose `origin` is the declared repository, at the reported commit.
Somebody or something looked.

**What it does not establish:** that the item works, is wired, or is reached
by any client. `remote-dev.vaws-ops-module` is `arrived` (the code is in
`lib/vaws_ops.py`) while `remote-dev.vaws-tool-provider` is `missing` (nothing
serves it). Evidence is chosen per row to make that distinction visible, and a
row's `notes` say what the evidence does and does not prove.

**What a recorded `arrived` on a private destination establishes in public
CI:** only that the named observer wrote it down after inspecting the named
commit. CI cannot re-check it and says so.

### Receipts (optional, destination-published)

A destination may publish a tracked receipt at `docs/split-receipt.json`:

```json
{"version": 1, "items": {"<ledger id>": {"arrived_in": "<commit>",
                                           "attested_by": "...", "attested_on": "YYYY-MM-DD"}}}
```

The receipt is the destination's own signed-off claim. It is read only from an
inspected checkout, so it changes nothing for a private destination in public
CI. It never produces `arrived` on its own: when the evidence is present the
row is marked `attested`; when the receipt attests an item whose evidence is
absent, that is a `receipt-contradiction` failure, because one of the two
records is wrong and neither should be trusted until they agree. No
destination publishes a receipt yet; the scaffold cannot add one for them.

---

## 3. The three gaps, as the ledger records them

Observed 2026-09-07 against `vaws-coordinator` `f7c0682` and `remote-dev`
`2ff1162`, both `main`.

### `remote-dev.managed-jobs-supervisor` (and `.managed-jobs-tests`)

remote-dev commit `900ad15`: "The supervisor moves to
`vllm-ascend-workspace/vaws-coordinator`", and deleted `core/managed_jobs.py`
with its eight tests. The coordinator's `docs/HANDOFF.md` §3 lists the same
file as an *external dependency on remote-dev*, and its `main` still reads it
from the remote-dev checkout (`lib/vaws_remote_dev.py`, `backend.py`). Each
handoff was internally consistent; together they left the child-subreaper
supervisor, the coordinator's headline guarantee, with no implementation in
either repository. Recorded `missing`; follow-up: coordinator pull request #1
(`fix/rehome-job-supervisor`) adds `workers/managed_jobs.py` and the suite.
Run against that branch, the checker fails with `arrived-not-recorded` on both
rows, which is the prompt to flip them once it merges.

### `remote-dev.vaws-tool-provider`

remote-dev commit `f30b992` dropped the four `vaws_*` tools and stated it "will
not grow a plugin hook for foreign tools". The coordinator owns the
implementation and schemas (`lib/vaws_ops.py`, row `remote-dev.vaws-ops-module`
is `arrived`), but its README still says "an MCP host (today the remote-dev
stdio server) registers them", and its own server registers nothing. A client
configured per the docs gets a server with none of those tools. Evidence: a
non-library, non-test module in the coordinator that wires `TOOL_SCHEMAS` or
`TOOL_DESCRIPTIONS` into a host. Recorded `missing`; follow-up is a design
decision owned by the coordinator, with no pull request observed.

### `remote-dev.task-facade-tests`

Six tests declared as moving to the coordinator (`f30b992`, handoff §5): two
`vaws.finish` outcome tests and four `vaws.py` CLI contract tests. The
coordinator's handoff lists the source file only for deletion. Present in
neither repository. Recorded `missing`; the coordinator owns the subjects.

### Found while populating the ledger

Four more declared moves have not arrived, all remote-dev → scaffold: the
three `FindSessionBindingTests`, `tools/sync_claude_skills.py` with its three
shim tests, and the managed-endpoint resolver plugin that replaces
`core/endpoint.py::_endpoint_from_managed`. None is broken *today*, because the
scaffold still carries the in-tree `.remote-dev/` copy; all four vanish the
moment that directory is replaced by the extracted repository. They are
recorded `missing` so the scaffold's own CI, which always inspects the
scaffold, keeps saying so until they land.

Eighteen rows are `arrived`, including the byte-pinned copies
(`vaws_build_inputs.py`, vendored `vaws_run_manifest.py`) whose digests match
both sides, and the `vaws-top` history whose head commit is an ancestor of the
new repository's `main`.

---

## 4. Registering a future extraction

Do this in the same pull request that deletes or moves the item, not after.

1. **One row per thing that leaves**, in `.agents/policy/split-ledger.json`.
   If the destination repository is new, add it under `repositories` first
   (slug, visibility, and `receipt_path` if it will publish one).
2. **Quote the declaration.** `declared_by` names the commit or handoff
   section and repeats what it says. If two documents disagree, list both;
   the disagreement is the finding.
3. **Choose evidence that would fail if the item quietly did not arrive.** A
   module: `symbols` with its public names. A test suite: `symbols` with the
   test names. A vendored copy: `sha256`. History: `commit`. A behaviour or
   registration with no fixed file name: `reference` with a pattern and an
   `exclude` list that rules out the implementation it wires.
4. **Record what you saw, not what you expect.** Clone the destination, run
   the checker with `--destination`, and write the verdict into `recorded`
   with the commit you inspected. If you could not look, write `unverified`
   and say why. If it is `missing`, name the `follow_up`.
5. Run `python3 -m unittest discover -s .agents/tests -p
   "test_split_reconcile_ledger.py"`.

When the destination later lands the item, the scaffold's CI (for public
destinations) or a maintainer's local run (for private ones) fails with
`arrived-not-recorded`. Flip the row in the scaffold, citing the new commit.
That red run is the mechanism working: nothing arrives unrecorded.

What the ledger does **not** track: scaffold-side call sites that must be
rewired to consume an extracted repository. Those are the boundary guard's R1
and R4 findings in `docs/repo-boundaries.md`.
