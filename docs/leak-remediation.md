# Leak remediation: what is exposed, and what the options cost

Status: **report only.** Nothing in this document has been acted on. No history
has been rewritten and nothing has been force-pushed. Rewriting the history of a
public repository with 35 forks and open pull requests is the owner's decision,
not an agent's.

This document deliberately does not restate the leaked values. Where a value is
needed to act, it is at the file and line given below. The scanner would flag
this document if it quoted them, which is the intended behaviour.

## Current tree after actual-main integration (2026-09-07)

Ordinary merge of actual published main
`257dc131c2015d0e01445288efb89bc5ab825b5f` (PR #90, tree
`dc18fcd13ae02468892d3097a226d92dbd302927`) deleted tracked `.remote-dev/**`.
The dated bodies in "What is exposed" remain historical evidence of the
pre-extraction snapshot used by #83; they are not a census of this merged
tree.

Current-tree status of those historical findings:

- Finding 1 (knowledge RFC 1918 range) is still in
  `.agents/knowledge/known-failure-signatures.yaml`. The
  `knowledge-failure-signatures-private-range` allowance stays.
- Finding 2 (`.remote-dev/DESIGN.md`) is absent from the current tree. The
  #83 wording change is history-only; extraction removed the file.
- Finding 3 (profiling command-recipes home path) remains the #83 placeholder
  in the current tree.
- Finding 4 (profiling shared-storage owner token) remains in that skill
  package and is still allowlisted for the later #88 cleanup. This G1
  integration does not apply that cleanup.
- Finding 5's `.remote-dev/README.md` occurrence and finding 6's
  `.remote-dev/tests/` placeholders are absent from the current tree. The
  skill-recipe example hosts remain.
- Allowlist entries whose complete owning paths were the extracted tree
  (`remote-dev-tests-placeholder-host`, `remote-dev-readme-example-host`)
  were removed. Remaining allowances were not dropped merely because a scan
  did not visit them.

The 13 current-main scanner findings on this merged tree were resolved
narrowly on 2026-09-07. Accepted envelope source, the feedback contract,
and result-envelope tests are unchanged. Shared-root names were not added
to global `allowed_absolute_path_prefixes`. No whole-file exclusion and no
real-secret allowance were added.

- The accepted envelope `_SAFE_HOME_PREFIXES` bare shared-root literals
  other than the already-global weights prefix are allowlisted only in
  `.agents/lib/vaws_result_envelope.py` and
  `docs/agent-feedback-contract.md`, each with one owning `path_glob` and
  an anchored four-name regex. The same names elsewhere, and longer or
  different home paths in those files, remain findings.
- Three exact fixtures in `.agents/tests/test_result_envelope.py` are
  allowlisted by one category and one detector match each (version-date
  identifier, synthetic example home, quoted synthetic secret-key
  literal). Changed values, other paths, and other categories at that
  path remain findings.
- The property run-manifest local field-label variable was renamed so it
  is no longer a secret-key assignment. No policy exception was added
  for it.

Full-tree scan after that G1-on-#90 candidate with its own policy was
469 scanned, 0 skipped, 0 findings, 116 suppressed, 0 unused allowlist
ids. The suppression count was two above the 12 converted current-main
matches because the policy file quotes two of those exact `match`
literals and `policy-self-reference` records them.

Ordinary merge of actual published coordinator-consumer main
`f38ea47bfb0e123b306723ac937f0514a0830169` (PR #98) on 2026-09-07
removed the in-tree `.agents/coordinator/tests/**` package. Combined
scan before the one remaining cleanup: 464 scanned, 0 skipped, 0
findings, 115 suppressed, 1 unused allowlist id
(`coordinator-test-bearer-token`). That stale path-scoped entry was
removed; the five current-main scoped allowances and the guard runtime
skip glob were kept. Combined scan after that removal: 464 scanned, 0
skipped, 0 findings, 115 suppressed, 0 unused allowlist ids.

Finding 4's profiling shared-storage owner token remains allowlisted
for the later #88 cleanup. This is not leak-proof coverage of skipped
files or of public history.

## Method

Counts come from walking every commit on `origin/main` (114 commits at the time
of writing) and testing the file's blob content, not from `git log -S`, which
only reports the commits that *changed* a value. Branch counts cover the
`origin/*` refs present in the working clone and are therefore a **lower
bound**: the remote has 27 heads, most of which branch from `main` and carry the
same blobs.

## What is exposed

### 1. Internal address range (highest topology value)

- **Where:** `.agents/knowledge/known-failure-signatures.yaml`, line 10, inside
  a free-text `applicable_versions` field.
- **What:** four consecutive host addresses in one RFC 1918 `192.168.x.0/24`
  subnet, together with the fact that they run TP2/TP4/TP8/TP16 vLLM-Ascend
  services on A3 containers.
- **History:** introduced on a feature branch on 2026-09-03; reached `main` in
  the merge of PR #71 on 2026-09-07. Present in **1 commit on `main`** (the
  current tip) and in at least **2 further pushed branch commits**
  (`origin/refactor/profiling-skills-v2`, `origin/feat/ascend-tensor-dump`,
  the latter being open PR #75). Every fork or clone taken after 2026-09-07
  contains it.
- **Actual risk:** low direct risk, non-trivial indirect risk. RFC 1918
  addresses are not routable from the internet, so nobody can reach these hosts
  with this information. What it does reveal is internal topology: the subnet in
  use, that four consecutive addresses are one NPU pool, and the parallelism
  layout of the services on them. That is reconnaissance value for anyone who
  already has, or later obtains, a foothold on the internal network, and it
  correlates with the session names in this repository (`session/*-153`,
  `*-154`, …), which encode the same host indices.
- **Not fixed here:** a parallel agent is migrating that file's schema and
  strips the range as part of that work. Editing it here would guarantee a
  conflict. The guard carries a scoped baseline entry
  (`knowledge-failure-signatures-private-range`) with a `remediation` note so
  the allowance disappears with the migration.

### 2. Personal laptop path

- **Where:** `.remote-dev/DESIGN.md`, line 4 — a `/Users/<person>/Downloads/…`
  reference to an untracked design draft.
- **History:** introduced 2026-05-25, present in **29 commits on `main`**
  through 2026-09-07.
- **Actual risk:** low. It discloses the maintainer's macOS account name (which
  matches the public GitHub account anyway) and the fact that a design document
  lived in `~/Downloads`. No credential, no host.
- **Fixed in this change:** the paragraph now states that the document is itself
  the authoritative design record, so it reads correctly without the path.

### 3. Personal remote path

- **Where:** `.agents/skills/ascend-profiling-analysis/references/command-recipes.md`,
  line 74 — a `/home/<employee-id>/transfer_dsv4` search root.
- **History:** introduced 2026-05-21, present in **38 commits on `main`**
  through 2026-09-07.
- **Actual risk:** low-to-moderate. The path segment is an employee-id-shaped
  token, i.e. a corporate identifier, and it pairs the identifier with a
  specific internal machine layout. It is the kind of value that makes internal
  accounts guessable.
- **Fixed in this change:** replaced with a `<remote-user>` placeholder plus a
  sentence explaining it.

### 4. Same employee id in shared-storage paths (not previously reported)

- **Where:** `.agents/skills/ascend-profiling-analysis/SKILL.md` (4
  occurrences) and
  `.agents/skills/ascend-profiling-analysis/tests/test_output_destinations.py`
  (2 occurrences), as `/mnt/weight/<employee-id>/profiling/…`.
- **History:** `SKILL.md` reached `main` in PR #71 on 2026-09-07 (**1 commit on
  `main`**); the test file arrived with the same PR.
- **How it was found:** the `internal-identifier` rule. The path is not under a
  home root, so a path-only rule would have missed it — this is the concrete
  reason the guard has an identity-token category.
- **Actual risk:** same class as finding 3, plus it names a shared 366 TB store
  mounted on every managed machine and container.
- **Fixed in the working tree** by the follow-up that landed after the
  parallel refactor of that skill package: the recipes and the example output
  in `SKILL.md` now use `/mnt/weight/<user>/profiling/…`, the placeholder the
  collection skill and `profile_analyze.py --archive-output` already used for
  this mount, with one sentence saying what to substitute; the unit test uses
  the neutral directory `/mnt/weight/profiling-shared/…`. The baseline entry
  (`profiling-analysis-shared-storage-owner`) was removed in the same commit.
  History exposure is unchanged and is covered by the options below.

### 5. Routable addresses used as documentation examples

- **Where:** `173.125.1.x`, `173.131.1.x`, `125.173.1.x` across
  `.agents/skills/machine-management/references/` (35),
  `.agents/skills/remote-toolbox/references/` (11),
  `.agents/skills/ascend-profiling-analysis/SKILL.md` (2),
  `.agents/skills/machine-management/scripts/manage_machine.py` (2),
  `.remote-dev/README.md` (1).
- **Actual risk:** unclear, and that is the problem. These are globally
  routable addresses in space assigned to real operators, used as if they were
  documentation examples. Either they are sanitized versions of a real jump
  host — in which case the digit shuffling is weak sanitization — or they are
  arbitrary, in which case the repository is publishing commands aimed at
  somebody else's address space.
- **Not fixed here:** out of the scope handed to this change, which named two
  specific path findings. They are baselined with `remediation` notes.
  Recommended follow-up, one mechanical commit:

  ```bash
  git grep -l -E '173\.(125|131)\.1\.[0-9]+|125\.173\.1\.[0-9]+' \
    | xargs sed -i '' -E 's/(173\.(125|131)|125\.173)\.1\.[0-9]+/192.0.2.10/g'
  ```

  then delete the four `*-example-host` entries from the allowlist.

### 6. Private and placeholder addresses in tests and recipes

The `1.2.3.x` dotted-quad placeholder (42 occurrences in `.remote-dev/tests/`)
and `10.0.0.x` (20 occurrences across skill tests and recipes). Both are
conventional placeholders; the former is nevertheless globally routable and the
latter is
RFC 1918. No exposure of this project's infrastructure. Baselined with a
`remediation` note recommending RFC 5737 values.

## Options for the history problem

Findings 1–4 are already in published history. Fixing the working tree — done
for 2 and 3 by the guard change and for 4 by its follow-up — stops the
bleeding but does not remove anything from history. The options, with their
real costs:

### Option A — do nothing about history (recommended for findings 2, 3, 5, 6)

- **Cost:** none technically. The values remain retrievable by anyone who clones
  or browses a fork.
- **Why it is defensible:** none of these are credentials. Rotating them is not
  a concept that applies. A macOS account name that matches the public GitHub
  account, and a placeholder address, are not worth breaking 35 forks over.
- **Residual action:** none beyond the working-tree fixes and the guard.

### Option B — do nothing about history, but neutralize the value (recommended for finding 1)

- **What:** let the in-flight knowledge migration remove the range from the
  working tree; separately confirm with whoever owns those hosts that nothing
  about them is reachable from outside, and treat the addresses as burned for
  future documentation.
- **Cost:** near zero.
- **Why:** RFC 1918 addresses cannot be attacked from the internet. The value
  of removing them from history is topology hygiene, and the price of rewriting
  is high (below). If the internal network's security depends on the subnet not
  being published, that is the actual problem to fix.

### Option C — rewrite history (`git filter-repo`) and force-push

- **What:** rewrite every affected commit (29–38 commits for findings 2 and 3,
  1 commit plus branch tips for findings 1 and 4), then force-push `main` and
  every affected branch.
- **Cost, concretely:**
  - Every commit SHA from the rewrite point forward changes. The 4 open pull
    requests (#62, #73, #74, #75) would need to be rebased or recreated; review
    threads anchored to old SHAs break.
  - All 35 forks keep the old objects. GitHub keeps unreachable objects
    accessible by SHA on the original repository too, so the value stays
    fetchable unless GitHub Support is asked to garbage-collect it.
  - Every existing clone and every agent worktree in this scaffold (there are
    many, by design) has to re-clone or hard-reset; anyone who pulls without
    resetting will re-introduce the old history.
  - Submodule pins and the session/worktree state under `.vaws-local/` reference
    commit ids that would no longer exist.
- **When it is worth it:** only if a real credential leaks — an SSH key, a
  token, a password. None of these findings is that.
- **If it is chosen anyway:** rewrite once for all findings together, announce
  the window, ask GitHub Support to expire the old objects on this repo and its
  fork network, and rotate anything the old objects could help an attacker
  reach.

### Option D — make the repository private, or split before publishing

Since the repository is being split into several public repos under an
organization, the split is the cheap moment to drop history: create the new
repos from a squashed, scanned snapshot instead of a filtered copy of this
history. That removes findings 1–6 from the *new* public surface at no cost,
and leaves this repository's history as the only place the values remain.

- **Cost:** loses per-file history in the new repos.
- **This is the recommendation if the topology exposure is considered material.**

## Recommendation

1. Land the guard (this change) so nothing new is added — that is the only part
   that is irreversible if skipped.
2. Let the knowledge migration remove finding 1 from the tree; drop the baseline
   entry with it.
3. Fix findings 5 and 6 in one mechanical follow-up commit per owning skill
   (finding 4 is done).
4. Do not rewrite history for any of findings 1–6. Revisit only if an actual
   credential is ever found in history.
5. If the org split proceeds, seed the new public repos from a scanned snapshot
   rather than from filtered history.
