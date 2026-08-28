# Ready runtimes and cooperative execution

This is an opt-in, shared Streamable HTTP MCP service, independent of
`vaws-top`. It reuses **idle prepared containers**, environments and native
artifacts. It does not keep model services resident: each development run
starts its service using a pinned code snapshot.

## Authority and scope

| State | Owner |
| --- | --- |
| Machine directory | `shared_inventory_path()` in the primary Git worktree, from PR #67 |
| Logical sessions, exclusive runtime bindings, reserved service ports, messages | One shared coordinator database |
| NPU tasks, queue, fences, activation and release | Existing `vaws_npu_coordination.py` on each physical host |
| Source edits and workflow outputs | The actual business worktree and its local state |

All participating clients, including independent clones, must connect to the
**same manager**. Linked worktrees share its default state directory via Git
common-dir; unrelated clones do not discover each other automatically. One
process holds `manager.lock`; do not deploy separate databases for the same
runtime pool. Use canonical host addresses and only register containers
dedicated to this pool, after resolving any old managed-session claims.

Legacy session-local `leases.json` remains a compatibility mechanism, not a
cross-workspace allocator. The new path does not create a second local NPU
lease: every request goes to the existing host authority. Direct SSH,
unmanaged containers and generic remote-dev writes remain outside cooperative
enforcement. An MCP fence is not an OS access-control boundary.

## Start the shared manager

Requires Python 3.10+ on the manager (CI uses 3.12), not torch/torch_npu.
Install `.agents/coordinator/requirements.txt` in a dedicated virtualenv.
The official MCP Python SDK is pinned to 2.1.1; the existing remote-dev MCP
server and tool names are unchanged.

Create a private, **untracked** access file under `.vaws-local/coordinator/`:

```json
{
  "principals": {
    "developer-a": {"sha256": "<64-hex SHA256 of a random bearer token>", "admin": false},
    "pool-operator": {"sha256": "<64-hex SHA256 of a different random bearer token>", "admin": true}
  }
}
```

Generate at least 32 random bytes for each token. Store the tokens only in
client secret configuration, never in tracked files. Set access-file mode
`0600`. Start one process:

```bash
python .agents/coordinator/server.py \
  --access-file /absolute/primary-worktree/.vaws-local/coordinator/access.json
```

It binds `127.0.0.1:8766/mcp`. Configure each MCP client with that HTTP endpoint
and `Authorization: Bearer <its token>`. Other machines can use authenticated
SSH forwarding to the same loopback service. This is a private cooperative
fleet service, not a public OAuth authorization server. Host/Origin checks
remain enabled. No monitor, Docker daemon on the manager, or model service is
required. Service-manager installation is an operator deployment step, not
performed by importing or running tests in this PR.

## Prepare once, outside the launch path

1. Use existing machine-management/session preparation and approved parity
   install commands to prepare an owned container. Do not adopt somebody
   else's active container or silently change host CANN/drivers.
2. Stop and verify **only its owned workers**. Keep the container running.
   Materialize clean parity snapshots of vLLM and vllm-ascend. Verify the exact
   environment combination on the target SoC; an image tag alone is not proof.
3. Provide an untracked preparation specification with `profile` and `files`.
   `profile` requires exact strings for `image_digest`, `soc`, `driver`, `cann`,
   `python_abi` (SOABI), `torch`, `torch_npu`, `vllm`, `vllm_ascend`, `compiler`;
   `build_env`, `launch_env`; an operator-reviewed `compatibility_evidence`
   reference; and `system_files` entries for actual CANN/driver version files
   (`{"path": "/absolute/path", "sha256": "..."}`). Include additional critical
   `.pth`, compatibility-library or compiler files when the profile needs them.
4. `files` explicitly enumerates every runtime output relative to the runtime
   root, mapping each path to a role. At minimum it needs `library` and
   `metadata` roles. For Ascend custom operators include the kernel library,
   `binary_info_config.json`, vendor metadata and associated binaries; the
   preparer owns that complete dependency list. Do not include CMakeCache.txt
   or symlinks into another worktree.
5. Inside the container, using remote-dev, run:

```bash
python .agents/coordinator/prepare_runtime.py attest \
  --root /vllm-workspace --spec /path/to/private-preparation-spec.json \
  --owned-workers-stopped
python .agents/coordinator/prepare_runtime.py publish \
  --root /vllm-workspace --cache /root/.cache/vaws/native-bundles \
  --owned-workers-stopped
```

Attestation checks installed versions/ABI/system-file hashes, executes import
smoke (`torch_npu`, `vllm`, `vllm_ascend`, `acl`), fingerprints native inputs and
writes `.vaws-runtime/ready-profile.json`. It does not prove model correctness,
all possible operator dependencies or multi-node compatibility. Bundles are
atomically published by profile/build identity and verified before reuse.
Restoration deliberately requires the same installation path; relocatability
of editable installs/native operators is not assumed.

An administrator then calls `runtime_register` with a unique runtime id and:

```json
{
  "machine": "<existing shared-inventory alias>",
  "container_name": "<owned prepared container>",
  "port": 46010,
  "root": "/vllm-workspace",
  "service_ports": [18010]
}
```

Alternatively use explicit `host_endpoint`, `endpoint` and `container_name`.
`machine_catalog` uses the same shared inventory loader as the toolbox;
`runtime_catalog` lists prepared identities. The old inventory's base container
is not automatically adopted. Registration/checkout verifies the Docker
identity, idle workers, free declared TCP ports, packages, input fingerprints
and complete output hashes. Uncertain probes produce a miss/repair state.
`service_ports: []` reserves no serving ports and is suitable for operator jobs.

## Agent development loop

1. `session_open` records the actual local source paths; it creates no business
   checkout and binds no machine. `runtime_checkout` selects an exact profile
   (optionally a specific runtime) with an idempotency key. The returned
   `host/port/user/root/cwd` fields work with existing remote-dev tools.
2. With no execution pending/active, synchronize using parity's low-level
   direct endpoint arguments and **`--apply-mode materialize`**. Add
   `--source vllm=/actual/worktree --source vllm-ascend=/actual/worktree` for
   external sources. This only updates source; it cannot install or compile.
   Export the returned environment fingerprint and the profile's build flags
   when computing parity build inputs. Do not use legacy `auto/install` as
   the pool's warm checkout path: a new client's local install history may
   otherwise request an unnecessary first rebuild.
3. Keep editing with the staging watcher:

```bash
python .agents/skills/remote-code-parity/scripts/parity_watch.py --interval 1 -- \
  --workspace-root /actual/scaffold --workspace-id task-a \
  --source vllm=/actual/vllm --source vllm-ascend=/actual/vllm-ascend \
  --server-name runtime-a --runtime-root /vllm-workspace \
  --container-identity prepared-a@/vllm-workspace \
  --container-host HOST --container-port PORT --container-user root
```

The watcher hashes content (including subsequent edits to the same dirty file)
and incrementally publishes Git objects to the container cache. It **never
materializes the runtime or builds**. A concurrent edit remains pending for
the next cycle. The running source tree stays fixed. Watcher output is JSONL
and is staging evidence, not a ready model endpoint.

4. `execution_request` pins the actual materialized `snapshot_commits`, expected
   `build_key`, exact `devices` or `npu_count` (`devices: []` for count mode),
   priority and queue deadline. Changed native inputs or missing output files
   fail before asking for cards. A compatible warm hit creates no container,
   installs no dependencies and performs no compilation.
5. For a cache miss, explicitly restore a matching bundle with
   `prepare_runtime.py restore --cache ... --build-key ...`, or use the
   existing installer to build only changed inputs, then attest/publish.
   `runtime_refresh` accepts this new preparation only when no execution is
   unresolved and the environment profile is unchanged. Building/preparing
   is an explicit separate operation, never hidden inside a launch.
6. Poll until granted; call `execution_control(action="preflight")` immediately
   before launch. Use the returned physical `ASCEND_RT_VISIBLE_DEVICES`, the
   binding's reserved service ports and `launch_preamble` through remote-dev.
   The preamble **prepends** PATH/PYTHONPATH/LD_LIBRARY_PATH instead of removing
   base acl/native-compat paths. Start a new owned service/job, activate with
   its PID promptly (do not wait for weight loading), and heartbeat while it
   runs. Readiness still needs all ranks and an actual model request.
7. Stop only this run's workers, then request `release`. The host must observe
   every leased device free in repeated samples. `runtime_return` quarantines
   the container until an administrator re-verifies it; it never kills a
   process or infers successful cleanup from a client timeout.

The manager exports Run Manifest v1 records under its untracked `runs/`
directory. It never marks a run `passed` merely because an allocation was
released. Domain validation workflows attach their own acceptance evidence.

## Cooperation and failure handling

`coordination_peers`, `coordination_message`, `coordination_reply` and cursor-based
`coordination_events` support cooperative yield requests. Messages carry sender
identity and are untrusted text, not commands or permission to stop a peer.
Accepting a request does not release devices. Clients must poll/listen; MCP
does not wake a paused agent automatically.

The resident manager advances the host queue and reconciles persisted requests
in bounded batches. It never sends implicit heartbeats for absent clients.
Lost replies are recovered using the same task id. A changed host epoch or a
missing previously submitted task stays `uncertain`; inspect real ownership
before manual reconciliation. Do not delete state to make a resource appear
available. Manager restart preserves bindings/events; the host's `/tmp` epoch
remains explicitly separate from that durable state.

## Acceptance boundary

CI uses the actual HTTP MCP SDK with two principals and the real SQLite host
protocol, but simulated container/occupancy probes. It covers competing
management roots, authentication, ownership, restart, message cursors, no hidden
provisioning, native inputs and complete-bundle corruption/missing-file cases.
This is not five-client product certification or Ascend hardware acceptance.

Remaining rollout gates: prepare a real owned pool slot, validate a warm-hit
edit/sync/restart cycle, run two isolated services on disjoint cards/ports,
and exercise K3's exact four-node topology with all-rank logs and a real request.
Multi-host atomic/gang allocation, pool auto-replenishment and transparent
adapters for every legacy domain wrapper are not implemented in this version.
No actual K3 CANN/vLLM version combination is shipped as verified in this PR.
