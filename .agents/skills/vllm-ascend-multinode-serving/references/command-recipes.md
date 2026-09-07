# Multi-node serving command recipes

All examples assume one `session-management` session per node, code parity
already established on every node, and the planner run from the workspace root.

## Four nodes, TP16 x DP4, one data-parallel rank per node

Sixty-four devices total, sixteen per node, internal load balancing with the
API server on the first node.

```bash
python3 .agents/skills/vllm-ascend-multinode-serving/scripts/multinode_plan.py plan \
  --node n153=192.168.13.153 \
  --node n154=192.168.13.154 \
  --node n155=192.168.13.155 \
  --node n156=192.168.13.156 \
  --tp 16 --dp 4 --dp-local 1 --ep 64 --expert-parallel \
  --data-nic enp210s0f0 \
  --model /home/weights/SomeModel \
  --api-port 8000 --dp-rpc-port 29550 --hccl-port-range 20000-20127 \
  --output .vaws-local/multinode-serving/demo/plan.json \
  --serve-args --max-model-len 8192 --gpu-memory-utilization 0.9
```

`--serve-args` consumes the rest of the command line and appends it verbatim to
every node's `vllm serve` invocation, so it must come last. This is the only
way flag-shaped values survive argument parsing; quoted JSON values such as
`--compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'` are re-quoted
correctly when the block is rendered.

Read `topology`, `warnings`, and each node's `data_parallel_ranks` before
launching. `world_devices` must equal the devices you actually hold.

## Two nodes, TP8 x DP4, two data-parallel ranks per node

Thirty-two devices total, sixteen per node.

```bash
python3 .agents/skills/vllm-ascend-multinode-serving/scripts/multinode_plan.py plan \
  --node a=10.0.0.1 --node b=10.0.0.2 \
  --tp 8 --dp 4 --dp-local 2 \
  --data-nic eth0 --model /home/weights/SomeModel \
  --output .vaws-local/multinode-serving/two-node/plan.json
```

Node `a` takes data-parallel ranks 0 and 1, node `b` takes 2 and 3.

## Render and run one node

Render each node's block to its own file, non-master nodes first:

```bash
PLAN=.vaws-local/multinode-serving/demo/plan.json
OUT=.vaws-local/multinode-serving/demo/blocks
mkdir -p "$OUT"
python3 .agents/skills/vllm-ascend-multinode-serving/scripts/multinode_plan.py render \
  --plan "$PLAN" --node n154 --what launch > "$OUT/n154.sh"
# ... repeat for n155, n156, then n153 (the master) last
```

Then dispatch **one explicit call per node** through `remote_bash` against that
node's session endpoint, feeding the block on stdin, as a background process
group whose PGID you keep. You need the PGID to stop only what you started.

Do not drive the dispatch from a shell loop over node names, hostnames, or
PIDs. Word splitting in that loop is a recurring failure here: the value
arrives at the remote command mangled and the launch dies with `hostname
contains invalid characters` on several ranks at once, which reads like a
cluster configuration problem rather than a quoting problem. One explicit
invocation per node costs four lines and removes the whole class.

## Identity probe across all nodes

```bash
python3 .agents/skills/vllm-ascend-multinode-serving/scripts/multinode_plan.py render \
  --plan "$PLAN" --node n153 --what identity
```

The probe text is identical for every node, so render it once and run it on
each. Diff the outputs pairwise. Any difference in commit, dirty-file count,
module path, or native-extension SHA256 stops the deployment.

## Readiness gate

The stages come from the plan itself:

```bash
python3 -c "
import json,sys
plan=json.load(open(sys.argv[1]))
for stage in plan['readiness_gate']:
    print(stage['stage'], '|', stage['target'])
    print('   ', stage['check'])
" "$PLAN"
```

Walk them in order against the master endpoint. Treat the final
`real-completion` stage as the boundary: before it passes, the deployment is
starting; after it passes, the deployment exists and may be measured.

## Hybrid load balancing

Only when an external balancer is already in front of the nodes.

```bash
python3 .agents/skills/vllm-ascend-multinode-serving/scripts/multinode_plan.py plan \
  --node a=10.0.0.1 --node b=10.0.0.2 \
  --tp 8 --dp 4 --dp-local 2 --lb-mode hybrid \
  --data-nic eth0 --model /home/weights/SomeModel
```

Every node now carries `--data-parallel-hybrid-lb` and its own API port, and
every node is a valid entry point. Under the default internal mode only the
master is.

## Stopping

Stop the process group you recorded, on the node you started it on, then
confirm the devices are free:

```bash
kill -TERM -- -"$PGID"   # then -KILL if it does not exit
npu-smi info             # confirm no residual HBM on the devices you held
```

Engine and worker processes outlive the API process. A quiet API port with busy
devices means orphans are still holding memory, and the next launch will fail
on allocation rather than on anything informative.
