# Knowledge evidence-chain validation

Validation date: 2026-08-11

Base revision: `upstream/main` at `35f795c`

## End-to-end lifecycle

`.agents/tests/test_knowledge_flow.py` exercises the public command-line
surfaces against a temporary simulated repository:

1. capture a verified synthetic candidate into a session-scoped pending queue;
2. flush it through the bounded `SessionEnd` hook without reading a transcript;
3. list and inspect the review candidate;
4. promote it to an `active` formal entry using stable regression-test evidence;
5. query the compact match and fetch the full entry by id;
6. recapture the same candidate and observe `already-promoted`;
7. deprecate the entry while retaining its history;
8. validate all formal knowledge documents.

The candidate, formal knowledge store, reviewed archive, pending queue, and
SessionEnd receipt all live under a temporary directory. The test snapshots
the branch's tracked knowledge YAML files and verifies their bytes are
unchanged after the flow. The fixture is explicitly synthetic and is never
promoted into the branch's formal knowledge store.

## Commands and results

```text
PYTHONDONTWRITEBYTECODE=1 python3 .agents/tests/test_knowledge_flow.py
1 test passed

PYTHONDONTWRITEBYTECODE=1 python3 .agents/tests/test_run_manifest.py
7 tests passed

PYTHONDONTWRITEBYTECODE=1 python3 .agents/tests/test_knowledge_memory.py
13 tests passed

PYTHONDONTWRITEBYTECODE=1 python3 .agents/tests/test_knowledge_hook.py
8 tests passed

PYTHONDONTWRITEBYTECODE=1 python3 \
  .agents/skills/curate-workspace-knowledge/tests/test_knowledge_curate.py
10 tests passed

PYTHONDONTWRITEBYTECODE=1 python3 .agents/scripts/knowledge_validate.py
status: passed; 6 formal documents validated
```

Result: **39/39 tests passed**, and the formal branch knowledge documents
remain valid with empty `entries` arrays.

# Federated v2 client validation

Validation date: 2026-09-07

Base revision: `origin/main` at `161fed1`, branch `feat/knowledge-v2-client`

## What was exercised

`.agents/tests/test_knowledge_v2_flow.py` drives the v2 lifecycle through the
same public command-line surfaces, in a temporary simulated repository:

1. capture a synthetic candidate with a concrete environment coordinate and
   observe the unknown dimensions being named rather than filled;
2. promote it into `<kind>.v2.yaml`, landing `unverified` with unresolved
   markers;
3. observe the export gate refusing it while a dimension is unresolved;
4. resolve each remaining dimension (bounded values and a stated `any` basis);
5. verify with a followable reference and a non-submitter confirmation;
6. export a proposal bundle, then re-export and observe the reported no-op;
7. query across `shared` / `project` / `candidate` with the shared cache both
   absent (degraded, reported) and present;
8. validate both generations.

`.agents/tests/test_knowledge_migration_v2.py` covers the v1 -> v2 conversion
against a fixture reproducing the real `applicable_versions` string, including
its internal address range: the address reaches neither the migrated entry nor
the report, and no version is invented from an image reference.

`.agents/tests/test_knowledge_redaction.py` pins both directions of the
redaction ruleset — what must be refused, and what must keep working, since a
rule that flags public repository names would simply be switched off.

## Commands and results

```text
$ python3 -m compileall -q .agents
exit=0

$ python3 -m unittest discover -s .agents/tests
Ran 132 tests in 5.307s
OK

$ python3 -m unittest discover -s .agents/skills/curate-workspace-knowledge/tests
Ran 22 tests in 0.107s
OK

$ python3 .agents/scripts/knowledge_validate.py
status: passed
v1_documents: 6, v2_documents: 1, v2_entries: 2
redaction: profile r1, problems [], export_blockers []
entries_awaiting_human_coordinate:
  gloo-init-container-hostname-missing-from-etc-hosts: cann, driver,
    python_abi, torch, torch_npu, vllm, vllm_ascend, model, execution_mode
  ssh-stream-over-shared-controlmaster-mux-dies-early: soc, cann, driver,
    python_abi, torch, torch_npu, vllm, vllm_ascend, model, topology,
    execution_mode

$ python3 .agents/scripts/knowledge_export.py \
    --origin-repo vllm-ascend-workspace/vllm-ascend-workspace --check
status: partial; exportable []; blocked 2 (unresolved coordinate dimensions)
```

Result: **154 tests passed** across the shared suite and the
`curate-workspace-knowledge` package. Nothing here ran on an NPU: every path
under test is contract logic.

Not validated: no upstream PR was opened against `vaws-knowledge` (its
`tools/` and `server/` packages are unpublished), and the shared cache was
only populated from a locally produced bundle, never from a network pull.
