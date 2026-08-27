# Remote-dev client compatibility

Updated 2026-08-27. One server and one schema set serve every client; model
credentials and provider settings are separate and are not changed by this setup.

## Configuration

Start each client in this repository. Register the MCP once per client, not once
per model:

| Client | Project configuration | Server identifier |
| --- | --- | --- |
| Kimi Code | `.mcp.json` | `remote-dev` |
| Claude Code, including DeepSeek V4 | `.mcp.json` | `remote-dev` |
| Cursor IDE / Cursor Agent | `.cursor/mcp.json` | `remote-dev` |
| Codex | `.codex/config.toml` | `remote_dev` |
| Grok Build | `.grok/config.toml` | `remote-dev` |

All entries launch `.remote-dev/mcp/server.py` with Python 3. Copy the Codex/Grok
`config.example.toml` to the adjacent `config.toml`, then replace the placeholder
checkout path. Actual machine-specific TOML files are ignored by Git. Do not add
model credentials to these files. Restart existing sessions after schema/config
changes so they rediscover tools.

Cursor's checked-in entry uses the same repository-relative server path as
`.mcp.json`; open the repository as the workspace and run Cursor Agent from its
root. Its [official MCP configuration](https://cursor.com/docs/mcp) documents
`${workspaceFolder}` interpolation, but Cursor Agent `2026.08.25-3e8eec8` passed
that expression literally to Python in a real startup check. The relative path
avoids that CLI incompatibility. Use `cursor-agent` explicitly when Grok also
provides an `agent` command; the shared MCP does not require changing either
client's shell aliases.

If Cursor's model cannot discover the tools, check **Customize > MCPs >
remote-dev** and enable the repository source. A disabled source can still be
listed in the UI; it is not evidence that the model can call it. Start a new
agent after enabling it. Cursor Agent separately requires server approval
(`cursor-agent mcp enable remote-dev`) and a valid client login; these are not
MCP schema problems.

Grok's automatic `.mcp.json` import depends on its Claude-import state; a native
`.grok/config.toml` avoids that ambiguity. Confirm project trust on first use.
Check registration with `grok inspect --json` and `grok mcp doctor remote-dev --json`,
then test `search_tool` in a real session: doctor success alone is insufficient.

Codex uses its native project configuration, following the
[official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
The project example uses `approval_policy = "on-request"` so interactive MCP
confirmation is possible. It does **not** automatically approve remote writes.
In a headless `codex exec` run, a pending MCP confirmation can appear as
`user cancelled MCP tool call`; that is not a schema rejection. A bounded,
pre-authorized automation can supply invocation-only approvals for its exact
tools, for example:

```sh
codex exec \
  -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
  -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
  '<explicitly authorized task with a bounded remote root>'
```

Do not put blanket remote-command approvals in shared configuration. Normal
interactive calls should use the client's confirmation flow.

## Shared compatibility contract

- Advertised tool names use `remote_read`, `remote_apply_patch`, etc. Grok 1.0.5
  can complete the MCP handshake and list dotted names in doctor, yet fail to
  register them in a model session. An isolated naming-only comparison confirmed
  that underscore names expose all 18 tools to `search_tool`.
- The dispatcher still accepts legacy `remote.read` and `remote.apply_patch`
  names. Result envelopes retain canonical dotted tool names. Kimi/Claude
  namespaced names remain `mcp__remote-dev__remote_read`; Grok discovers
  `remote-dev__remote_read`; Codex registers the same tool under `remote_dev`.
- Input schemas are ordinary typed objects without `anyOf`, `oneOf`, `allOf`,
  or `$ref`. Kimi's provider rejects the former root `type` plus `anyOf` shape
  before the requested tool is executed, including on an ordinary chat prompt.
- Conditional requirements are documented in schema descriptions and enforced
  by the existing server: supply `host` + `port`, an alias, or a managed selector;
  supply non-empty `patch` or legacy `command` for patches. `patch` takes
  precedence. Missing endpoint/payload is rejected before remote execution.
- `remote_multi_edit.edits` exposes typed item fields; omitted `new_string`
  retains the existing empty-string deletion behavior.
- Path containment, symlink checks, read ledgers, patch atomicity, and SSH
  execution behavior are unchanged. No client-specific schema fork is needed.

## Verification, 2026-08-27

PR #64 code/config revision `1f93400` was exercised from the VAWS checkout. Each
model performed four sequential MCP calls in a newly created remote scratch
directory: patch a new file, read it, replace its content with `remote_multi_edit`,
and read it again. Final content and SHA256 were independently checked remotely.
A successful process exit or a server listed as connected does not count as a
passing test.

| Client / model | Version | Four-call result |
| --- | --- | --- |
| Kimi Code / `kimi-for-coding` | 0.38.0 | Passed, `VAWS_PR64_kimi_clean_OK` |
| Claude Code / `deepseek-v4-flash` | 2.1.143 | Passed, `VAWS_PR64_claude_OK` |
| Codex / `gpt-5.6-sol` | 0.147.0 | Passed, `VAWS_PR64_codex_OK` |
| Grok Build / `grok-4.6` (`grok-4.6-build` usage ID) | 1.0.5 | Discovery and calls passed, `VAWS_PR64_grok_OK` |
| Cursor IDE / Cursor Grok 4.6 | 3.17.19 | Discovery and calls passed, `VAWS_PR64_cursor_OK` |

The initial Kimi attempt incorrectly escaped patch newlines; the server rejected
that payload without writing, and the model corrected it. A fresh-file rerun
with an explicit multiline patch passed all four calls without a retry.

Cursor Agent `2026.08.25-3e8eec8` loaded all 18 tools with the relative path, but
its model request failed because its saved login had expired. That CLI model run
is **not** counted as passing: the completed Cursor row is the native IDE test,
using its existing authenticated session after enabling the workspace MCP source.

Grok and Codex headless checks used invocation-scoped approvals for only
`remote_apply_patch`, `remote_read`, and `remote_multi_edit`. No blanket approvals
were persisted. The same server code also passed a separate Codex patch/read
test with normal allow-once interactive confirmations.

Remote Python 3.9.9 unit validation:

- 63 focused tests pass: MCP schema/framing/aliases, native Cursor entry, missing
  arguments, patch atomicity/path guards, endpoints, read/write/edit, artifacts,
  hooks, and CLI wrappers/errors. The same 63 tests pass in GitHub Actions on
  both Python 3.9 and 3.12.
- MCP burden validation passes: 18 tools, 18 CLI wrappers, no missing documented
  endpoint requirements, maximum 3 tool-specific required arguments.
- Full suite: 82 tests, 8 failures within the two pre-existing Claude skill-shim
  checks in `test_cli_help.py`. Base commit `2176b48` runs 73 tests and reproduces
  exactly the same 8 failures. No unrelated skill packages were changed to mask
  this baseline.

The focused CI command is in `.github/workflows/remote-dev.yml`; the full suite
can be reproduced with `python3 -B -m unittest discover -s .remote-dev/tests` on
a remote validation host. These unit tests use temporary files and mocked
transports and do not require NPU access.

These checks establish this MCP integration at the versions above, not every
provider feature, arbitrary future versions, or NPU/model-serving correctness.
No existing containers, NPU workloads, model weights, or runtime checkouts were
modified by the smoke tests.
