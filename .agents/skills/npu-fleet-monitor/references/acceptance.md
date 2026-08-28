# Deployment acceptance

Accept an `ensure` or `restart` only when all of the following are true:

- the selected worktree is attached to the requested branch and tracked files are clean;
- `package.json`, `scripts/install-user-service.sh`, and the built `dist/client` are present;
- `npu-fleet-monitor.service` reports `ActiveState=active` and `SubState=running`;
- `http://127.0.0.1:8789/api/health` returns `status=ok` without using an HTTP proxy;
- the health payload reports `mode=idle` and the configured idle interval when no browser lease exists;
- the service unit and dashboard URLs remain bound to loopback.

The overview can initially show `pending` while the first fleet probe completes. A healthy API is sufficient for process readiness; verify `/api/overview` separately when the request requires current fleet coverage.

For failures, inspect the final JSON first, then use:

```bash
systemctl --user status npu-fleet-monitor.service
journalctl --user -u npu-fleet-monitor.service -n 100 --no-pager
```

Run these on the host execution plane. Do not work around OpenSSH configuration failures with `ssh -F /dev/null`, and do not expose passwords or the monitor's `data/keys` contents in diagnostics.
