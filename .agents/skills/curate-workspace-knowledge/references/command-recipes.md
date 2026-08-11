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

Promote a novel candidate:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  promote --candidate-id <candidate-id> --entry-id <formal-id> \
  --status experimental
```

Merge a matching candidate:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  merge --candidate-id <candidate-id> --entry-id <formal-id>
```

Reject a candidate:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  reject --candidate-id <candidate-id> --reason "<reason>"
```

Deprecate a formal entry without deleting history:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  deprecate --entry-id <formal-id> --superseded-by <replacement-id> \
  --reason "<reason>"
```
