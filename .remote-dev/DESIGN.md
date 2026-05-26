# Remote Developer Substrate Design

This package implements the design from
`/Users/maoxx241/Downloads/remote_dev_substrate_design_for_codex.md`.

## Architecture

Layer A is the remote-native developer tool surface:

- RemoteRead / `remote.read`
- RemoteWrite / `remote.write`
- RemoteEdit / `remote.edit`
- RemoteMultiEdit / `remote.multi_edit`
- RemoteBash / `remote.bash`
- RemoteGlob / `remote.glob`
- RemoteGrep / `remote.grep`
- RemoteLS / `remote.ls`
- RemoteMonitor / `remote.monitor`
- RemoteApplyPatch / `remote.apply_patch`

Layer B is the shared substrate:

- endpoint resolution
- SSH transport
- full-permission default root with optional explicit root/cwd path policy
- optional read-ledger concurrency checks
- compact previews plus full refs
- background job registry
- artifact manifests and pull verification
- Claude/Codex hook guards
- MCP server

Layer C is the vLLM-Ascend workflow layer under `.agents/skills/`. Those skills
should consume remote-dev tools instead of teaching agents a separate remote
interaction model.

## Implementation Phases

Phase 0 establishes schemas, result contracts, endpoint identity, path policy,
and hook tests.

Phase 1 implements `remote.bash`, `remote.read`, and `remote.ls`.

Phase 2 implements `remote.write`, `remote.edit`, and `remote.multi_edit` with
default write/edit permission, optional read-ledger concurrency checks, and
atomic writes.

Phase 3 implements `remote.apply_patch` for Codex apply_patch payloads,
including file moves and end-of-file markers, plus unified diffs.

Phase 4 implements search, monitor/jobs, and artifact manifest/pull/push tools.

Phase 5 adds MCP, Claude/Codex configuration examples, hook guards, and
generated lightweight Claude skill shims.

The MCP server supports standard stdio `Content-Length` JSON-RPC framing. The
newline-delimited JSON-RPC mode is retained only as a lightweight local test
fallback.

MCP resources expose endpoint index/context, job registries and bounded
stdout/stderr reads, and local artifact manifests:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/status`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stdout`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stderr`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

Phase 6 keeps existing VAWS wrappers as compatibility backend while
vLLM-Ascend skills are progressively rewritten as remote-dev consumers.

Generated Claude Code skill shims are checked with:

```bash
python3 .remote-dev/tools/sync_claude_skills.py --check
```

## Validation

Expected scaffold checks:

```bash
python3 -m compileall -q .agents .remote-dev
python3 -m unittest discover -s .remote-dev/tests
python3 -m unittest discover -s .agents/tests
python3 .remote-dev/tools/validate_remote_dev_scaffold.py --local-only
```

Remote endpoint behavior requires a reachable SSH endpoint or managed VAWS
session. Use `validate_remote_dev_scaffold.py` with either `--host/--port` or
`--session-id` to run the live smoke, including parallel scratch workers.
