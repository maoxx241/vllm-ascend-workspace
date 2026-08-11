# Graph debug command recipes

Run commands from the workspace root.

## Initialize a case

```bash
python -B .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py init \
  --case-dir .vaws-local/graph-debug/graph-case-001 \
  --case-id graph-case-001 \
  --stage accuracy \
  --eager-result pass \
  --graph-result fail \
  --reproduction "fixed prompt, seed 7, temperature 0, max_tokens 32" \
  --workspace-snapshot '{"workspace":"<commit-or-snapshot>","dirty":false}' \
  --environment '{"machine":"<alias>","cann":"<version>","torch_npu":"<version>"}' \
  --model '{"path":"<remote-model-path>"}' \
  --topology '{"tp":2,"devices":[0,1]}'
```

Never place secrets in JSON arguments.

## Record one controlled experiment

```bash
python -B .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py record \
  --case-dir .vaws-local/graph-debug/graph-case-001 \
  --variable "capture size: 128 -> 64" \
  --hypothesis "divergence follows padding introduced by capture size 128" \
  --expected "capture size 64 removes the first divergence" \
  --observed "first divergence remains at step 0 layer 3 rank 1" \
  --conclusion "capture size alone is excluded" \
  --next-step "compare rank-local attention metadata before layer 3"
```

## Compare eager and graph snapshots

```bash
python -B .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py compare \
  --case-dir .vaws-local/graph-debug/graph-case-001 \
  --eager /path/to/eager.jsonl \
  --graph /path/to/graph.jsonl \
  --atol 1e-5 \
  --rtol 1e-4
```

Inspect `first_divergence` in stdout first. Load the saved comparison artifact only when more detail is needed.

## Finalize

```bash
python -B .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py finalize \
  --case-dir .vaws-local/graph-debug/graph-case-001 \
  --root-cause "rank-local slot mapping was not refreshed before replay" \
  --fix "copy slot mapping into the fixed graph input buffer before replay" \
  --minimal-result pass \
  --original-result pass \
  --cleanup-status removed
```
