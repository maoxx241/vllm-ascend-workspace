# Remote Developer Substrate

`.remote-dev/` makes remote development feel like local development for Codex,
Claude Code, and other MCP-capable agents. Local work should still use native
Read/Edit/Write/Bash/Glob/Grep/apply_patch tools. Remote work should use the
matching remote companion tools and only add endpoint fields.

Default endpoint fields:

- `host`
- `port`
- `user`, default `root`
- `root`, default `/` (full remote path permissions by default)
- `cwd`, default `/vllm-workspace`

Primary tools:

- `remote.read`
- `remote.write`
- `remote.edit`
- `remote.multi_edit`
- `remote.bash`
- `remote.glob`
- `remote.grep`
- `remote.ls`
- `remote.monitor`
- `remote.apply_patch`
- `remote.job_status`
- `remote.job_tail`
- `remote.job_stop`
- `remote.artifact_manifest`
- `remote.artifact_pull`
- `remote.artifact_push`
- `remote.context_snapshot`
- `remote.probe`

The MCP server is `.remote-dev/mcp/server.py`. CLI fallbacks live under
`.remote-dev/tools/` and return a JSON object with a human-readable `text`
field and `remote-dev.result.v1` metadata in `result`. The MCP server supports
standard stdio `Content-Length` framing and a newline-delimited JSON-RPC fallback
for simple tests.

MCP resources expose endpoint state and generated evidence:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/status`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stdout`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stderr`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

Runtime state is local and untracked under `.remote-dev/state/`. Endpoint alias
files are:

- `.remote-dev/endpoints.json` for team-safe aliases with no secrets.
- `.remote-dev/endpoints.local.json` for local aliases and ignored state.

Managed VAWS `session_id`, `session_file`, and `machine` resolution remain
available as compatibility modes. Host plus port is the default remote-dev
surface.

Remote read ledgers are scoped by `client_context_id` when supplied, then by
`CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`, `CODEX_RUN_ID`, and
`REMOTE_DEV_SESSION_ID`. The MCP server sets `REMOTE_DEV_SESSION_ID` to a
process-local value on startup, so MCP clients do not need to pass
`client_context_id` explicitly. CLI fallback calls without a client/session id
use the default scope. Ledgers are used as optimistic concurrency checks when
available; they are not required for default edit/write permission.

Model-visible output is capped. Full command logs and job output are exposed
through result refs and MCP resources instead of being copied into tool text.

Claude Code skill shims are generated from `.agents/skills`:

```bash
python3 .remote-dev/tools/sync_claude_skills.py
python3 .remote-dev/tools/sync_claude_skills.py --check
```

Scaffold validation is available as one JSON-reporting entry point:

```bash
python3 .remote-dev/tools/validate_remote_dev_scaffold.py --local-only
python3 .remote-dev/tools/validate_remote_dev_scaffold.py --host 173.131.1.2 --port 46000 --root /vllm-workspace --cwd /vllm-workspace
python3 .remote-dev/tools/validate_remote_dev_scaffold.py --session-id <session-id> --root /vllm-workspace --cwd /vllm-workspace --skip-local
```

The validator runs local contract gates, reports MCP/CLI burden metrics, and can
exercise a live endpoint or session with remote read/edit/write/bash/search,
patch, artifacts, jobs, MCP resources, and parallel scratch workers.
