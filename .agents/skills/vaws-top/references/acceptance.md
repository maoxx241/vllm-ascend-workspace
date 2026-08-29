# Acceptance

## Deployment

Accept a deployment or restart only when:

- the selected worktree is on `vaws-top` and tracked files are clean;
- `package.json`, the platform service installer, and built `dist/client` are present;
- the managed service is running;
- `http://127.0.0.1:8789/api/health` returns `status=ok` without using an HTTP proxy;
- `/api/agent/servers` returns compact JSON with `source=cache`;
- with no browser lease, health reports `mode=idle` and the configured idle interval;
- API and dashboard listeners remain bound to loopback.

The fleet can temporarily contain `pending` entries while its first collection finishes. Preserve ignored history and key data during rebuilds.

## Agent interface

For CLI/MCP changes, validate:

- backend tests, lint, and production build;
- one cached CLI query, which must not trigger collection;
- one bounded live query that returns a newly assigned scheduler snapshot;
- one stdio MCP initialize, `tools/list`, and `tools/call` exchange;
- compact process/container/owner output on an occupied host;
- compact mount output and complete structured mount data.

For service failures, inspect health and recent service logs without printing passwords or files under `data/keys`.
