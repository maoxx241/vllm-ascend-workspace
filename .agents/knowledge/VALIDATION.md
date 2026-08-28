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
