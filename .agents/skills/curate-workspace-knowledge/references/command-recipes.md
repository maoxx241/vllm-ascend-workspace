# Command recipes

Inspect available package commands:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py --help
```

Prepare one candidate after reviewing its text and related query results:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py prepare \
  --candidate .vaws-local/knowledge/candidate/observation.md \
  --state-root .vaws-local/knowledge/contribution \
  --public-root .vaws-local/knowledge/public
```

Inspect `submit --help` for transport arguments and provide the actual upstream,
fork and Git checkout. Inspect `review --help` for the exact candidate head,
base and corpus commit. Never substitute a content digest for a Git commit.
