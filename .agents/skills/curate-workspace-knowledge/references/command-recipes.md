# Command recipes

List compact candidates:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py list
```

Inspect one candidate and possible matches:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  inspect --candidate-id <candidate-id>
```

Promote a novel candidate to the federated v2 document (default):

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  promote --candidate-id <candidate-id> --entry-id <entry-slug> \
  --origin-repo <owner>/vllm-ascend-workspace --contributor <handle>
```

The result lists `needs_human_input`; the entry stays `unverified` and
non-exportable until those dimensions are resolved.

Promote into the legacy v1 envelope instead:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  promote --schema 1 --candidate-id <candidate-id> --entry-id <formal-id> \
  --status experimental
```

List v2 entries blocked on a human coordinate:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  list-unresolved
```

Resolve one dimension — from a real run, a bound, or a stated independence
basis:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  resolve --entry-id <entry-slug> --dimension cann --values 8.2.RC1

python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  resolve --entry-id <entry-slug> --dimension vllm_ascend \
  --min 0.11.0rc1 --max 0.11.0

python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  resolve --entry-id <entry-slug> --dimension model \
  --any-basis "reproduced on Qwen3-8B and DeepSeek-V2-Lite"
```

Record independent confirmation and move the entry to `verified`:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  verify --entry-id <entry-slug> \
  --evidence pull_request:<owner>/<repo>#<number> \
  --verified-by <reviewer-handle> \
  --env soc=Ascend910_93 --env cann=8.2.RC1 --env driver=25.0.rc1.1 \
  --env torch=2.7.1 --env torch_npu=2.7.1.dev20250724 \
  --env vllm=0.11.0 --env vllm_ascend=0.11.0rc1
```

Merge a matching candidate (v1 entries only):

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  merge --candidate-id <candidate-id> --entry-id <formal-id>
```

Reject a candidate:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  reject --candidate-id <candidate-id> --reason "<reason>"
```

Deprecate a formal entry without deleting history (add `--schema 1` for a
legacy v1 entry):

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  deprecate --entry-id <entry-slug> --superseded-by <replacement-slug> \
  --reason "<reason>"
```

Check, then produce, an upstream proposal bundle:

```bash
python3 .agents/scripts/knowledge_export.py \
  --origin-repo <owner>/vllm-ascend-workspace --check

python3 .agents/scripts/knowledge_export.py \
  --origin-repo <owner>/vllm-ascend-workspace --contributor <handle> \
  --entry <entry-slug>
```

The shared layer is the corpus inside the installed `vaws-knowledge`
package. After `uv sync`, query it directly (or call the `vaws-knowledge`
MCP tools `knowledge_query` / `knowledge_explain` / `knowledge_capture`):

```bash
python3 .agents/scripts/knowledge_query.py --query Ascend910B4 --include-unverified --limit 1
```

Convert the remaining v1 documents and report what needs human input:

```bash
python3 .agents/scripts/knowledge_migrate_v2.py \
  --origin-repo <owner>/vllm-ascend-workspace --contributor <handle> \
  --report .agents/knowledge/MIGRATION-v2.md --dry-run
```
