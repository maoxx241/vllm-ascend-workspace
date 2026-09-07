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
| `path` | the file or directory exists as a regular blob or tree in the selected commit |
| `symbols` | one Python file (at `path`, or matching `glob` minus `exclude`) *defines* every listed name, found by AST, so a comment or string does not count |
| `sha256` | the file is byte-identical to a pinned digest (vendored copies) |
| `text` | the file contains a literal marker |
| `commit` | the commit is an ancestor of the selected destination revision (history moves) |
| `reference` | some file matching `glob` minus `exclude` matches a regex (a registration or call site exists somewhere) |

Globs are `fnmatch` patterns over checkout-relative paths; `*` crosses `/`.
`scan.skip_roots` (the two submodules, `node_modules`, untracked state) are
never searched. Git symlinks and submodule gitlinks are not followed; they are
`unverified` rather than present. Untracked or dirty working-tree bytes are
never evidence.

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

Optional preview of an already-local branch or PR commit (candidate evidence
only, never recorded as mainline publication):

```bash
python3 .agents/scripts/split_reconcile.py \
  --destination vaws-coordinator=/path/to/vaws-coordinator \
  --revision vaws-coordinator=abc1234
```

(or `VAWS_SPLIT_DESTINATIONS="name=path:name=path"`).

For each checkout, including the scaffold (`checkout: "."`), the checker
resolves the git top-level and a full commit SHA **once** before evaluating
any row. It refuses, as `unverified` with a reason, a non-git directory, a
nested subdirectory masquerading as the checkout root, missing git, a
missing/unusable commit, a missing origin, or an origin that is not the
declared repository. A destination with no checkout is `unverified`.

Origin identity is an exact GitHub `owner/repo` match on a supported host
(`github.com`), from HTTPS or SSH forms. A URL that merely ends with the same
owner/repo suffix on another host is not a match. Origin URLs, including any
credentials, are never copied into progress or result JSON.

**Publication versus candidate snapshot.** The normal path inspects the
already-fetched `refs/remotes/origin/<declared default_branch>` commit and
records that exact SHA. It does not fetch. If that ref is missing, the row is
`unverified`; a feature-branch `HEAD` is not silently treated as published
main. `--revision NAME=COMMIT` selects an already-local commit instead and
reports `revision_scope: candidate`. A candidate may prove presence at its
own SHA. Ledger mainline arrival updates still require evidence at the
published default-branch commit.

**Immutable tree.** Every content predicate (`path`, `sha256`, `text`, AST
`symbols`, regex `reference`, glob/exclude) and any receipt is read from git
objects at that selected SHA. `commit` evidence is ancestry of that SHA, not
of mutable `HEAD`. Moving `HEAD` or refs after snapshot selection does not
change the evidence used in that run.

**Unknown versus absent.** A known valid tree that lacks the declared
evidence is `missing`. Unknown repository, revision, or blob access is
`unverified`.

So, in each context:

| Where | Can read | `arrived` rows for private destinations |
|-------|----------|------------------------------------------|
| Public CI (`split-reconcile.yml`) | scaffold + the public destinations, checked out without credentials at the fetched default branch | reported `unverified`; the recorded state and its attribution are shown, not confirmed |
| A maintainer with org access, locally | everything | confirmed or contradicted at the selected immutable commit |

**What an `arrived` verdict establishes:** the declared evidence was found in
the declared GitHub owner/repository identity at the selected immutable
commit. Somebody or something looked at that tree.

**What it does not establish:** that the item works, is wired, or is reached
by any client, or that the origin string is cryptographic proof of
publication. A Git origin is local identity metadata. Root records the actual
API-observed publication SHA during final acceptance. Evidence is chosen per
row to make distinctions visible: `remote-dev.vaws-ops-module` is the
implementation in `lib/vaws_ops.py`, while `remote-dev.vaws-tool-provider` is
a non-library `TOOL_SCHEMAS` / `TOOL_DESCRIPTIONS` registration reference. A
row's `notes` say what the evidence does and does not prove. Source-presence
at a commit is not runtime, client, test, or deployment evidence.

**What a recorded `arrived` on a private destination establishes in public
CI:** only that the named observer wrote it down after inspecting the named
commit. CI cannot re-check it and says so. An enforce run may still succeed
with explicit `unverified` rows for unavailable private destinations; that
aggregate success is not verified arrival.

### Receipts (optional, destination-published)

A destination may publish a tracked receipt at `docs/split-receipt.json`:

```json
{"version": 1, "items": {"<ledger id>": {"arrived_in": "<commit>",
                                           "attested_by": "...", "attested_on": "YYYY-MM-DD"}}}
```

The receipt is the destination's own signed-off claim. It is loaded only from
the same committed snapshot as the evidence. An untracked or dirty working-tree
receipt cannot attest arrival or create a contradiction at that commit. The
receipt never produces `arrived` on its own: when the evidence is present the
row is marked `attested`; when the committed receipt attests an item whose
evidence is absent, that is a `receipt-contradiction` failure, because one of
the two records is wrong and neither should be trusted until they agree.
Malformed committed receipts stay explicit errors. No destination publishes a
receipt yet; the scaffold cannot add one for them.

---

## 3. The original gaps, and the current published-main snapshot

The live ledger records **26 arrived / 0 missing / 0 unverified** at the
exact published commits below. That count is source-presence in the declared
GitHub owner/repository identity at those immutable commits. It is not
runtime validation, client reachability, tests passing, or deployment, and a
Git origin string is local identity metadata rather than cryptographic proof
of publication. If published main advances, refresh this snapshot before
treating it as current.

### Original observation (2026-09-07)

Observed 2026-09-07 against `vaws-coordinator` `f7c0682` and `remote-dev`
`2ff1162`, both `main`. At that snapshot eight rows were recorded `missing`
and eighteen were `arrived`. The historical mismatch is kept here; it is not
the live ledger state.

### `remote-dev.managed-jobs-supervisor` (and `.managed-jobs-tests`)

remote-dev commit `900ad15`: "The supervisor moves to
`vllm-ascend-workspace/vaws-coordinator`", and deleted `core/managed_jobs.py`
with its eight tests. The coordinator's `docs/HANDOFF.md` §3 listed the same
file as an *external dependency on remote-dev*, and at `f7c0682` its `main`
still read it from the remote-dev checkout (`lib/vaws_remote_dev.py`,
`backend.py`). Each handoff was internally consistent; together they left the
child-subreaper supervisor, the coordinator's headline guarantee, with no
implementation in either repository. Recorded `missing` at `f7c0682`;
follow-up then: coordinator pull request #1 (`fix/rehome-job-supervisor`)
adds `workers/managed_jobs.py` and the suite. Run against that branch, the
checker fails with `arrived-not-recorded` on both rows, which is the prompt
to flip them once it merges.

### `remote-dev.vaws-tool-provider`

remote-dev commit `f30b992` dropped the four `vaws_*` tools and stated it "will
not grow a plugin hook for foreign tools". The coordinator owns the
implementation and schemas (`lib/vaws_ops.py`, row `remote-dev.vaws-ops-module`
was already `arrived` at `f7c0682`), but its README still said "an MCP host
(today the remote-dev stdio server) registers them", and at `f7c0682` its own
server registered nothing. A client configured per the docs got a server with
none of those tools. Evidence: a non-library, non-test module in the
coordinator that wires `TOOL_SCHEMAS` or `TOOL_DESCRIPTIONS` into a host.
Recorded `missing` at `f7c0682`; follow-up then was a design decision owned
by the coordinator, with no pull request observed at that commit.

### `remote-dev.task-facade-tests`

Six tests declared as moving to the coordinator (`f30b992`, handoff §5): two
`vaws.finish` outcome tests and four `vaws.py` CLI contract tests. The
coordinator's handoff listed the source file only for deletion. Present in
neither repository at `f7c0682`. Recorded `missing`; the coordinator owns the
subjects.

### Found while populating the ledger

Four more declared moves had not arrived at scaffold `161fed1`, all
remote-dev → scaffold: `remote-dev.find-session-binding-tests` (the three
`FindSessionBindingTests`), `remote-dev.sync-claude-skills`
(`tools/sync_claude_skills.py`) with `remote-dev.claude-shim-tests`, and
`remote-dev.managed-endpoint-resolver` (the plugin that replaces
`core/endpoint.py::_endpoint_from_managed`). None was broken at that
scaffold snapshot, because the tree still carried the in-tree `.remote-dev/`
copy; all four would vanish the moment that directory was replaced by the
extracted repository. They were recorded `missing` so the scaffold's own CI,
which always inspects the scaffold, would keep saying so until they landed.
Do not treat a local #90 branch as published main.

Eighteen rows were `arrived` at that original observation, including the
byte-pinned copies (`vaws_build_inputs.py`, vendored `vaws_run_manifest.py`)
whose digests match both sides, and the `vaws-top` history whose head commit
is an ancestor of the new repository's `main`.

### Current published-main snapshot (2026-09-07, after #90)

Re-evaluated with the L1 corrected checker against already-fetched
`refs/remotes/origin/main` at these exact commits, with no fetch, no
destination edits, and no receipts:

| Destination | Identity | Published `origin/main` |
|-------------|----------|-------------------------|
| scaffold | `maoxx241/vllm-ascend-workspace` (transfer has not happened) | `257dc131c2015d0e01445288efb89bc5ab825b5f` |
| `vaws-coordinator` | `vllm-ascend-workspace/vaws-coordinator` | `2e16e894e31a12d85a11117a2772031f30fdfebe` |
| `vaws-top` | `vllm-ascend-workspace/vaws-top` | `e13478484b9f52e8847169a785eebc32b268787f` |

All **26** declared rows are recorded `arrived` at those commits. The eight
rows whose previous `missing` records were stale:

* Coordinator, missing at `f7c0682`, present at `2e16e894e31a12d85a11117a2772031f30fdfebe`:
  `remote-dev.managed-jobs-supervisor`, `remote-dev.managed-jobs-tests`,
  `remote-dev.vaws-tool-provider`, `remote-dev.task-facade-tests`.
* Scaffold, missing at `161fed1`, present at `257dc131c2015d0e01445288efb89bc5ab825b5f`
  after #90 landed on actual published main:
  `remote-dev.find-session-binding-tests`, `remote-dev.sync-claude-skills`,
  `remote-dev.claude-shim-tests`, `remote-dev.managed-endpoint-resolver`.

The independent Git-object observation in the root handoff agrees: 26/26
arrived, those eight old `missing` entries drift. Helper or test definitions
on this #91 candidate are not evidence that a moved runtime consumer arrived;
the scaffold rows cite published main `257dc131c2015d0e01445288efb89bc5ab825b5f`,
not this merge commit.

Those eight recorded `observed_commit` values stay at that dated snapshot. A
later enforce run selects the checkout's current `refs/remotes/origin/main`;
that live `selected_commit` may advance and is not the recorded observation.

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
