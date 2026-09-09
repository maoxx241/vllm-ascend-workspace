# Command recipes

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --host 10.0.0.1 \
  --manifest .vaws-local/ascend-profiling-collection/runs/<run>/manifest.json

python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --execution-id <id> --remote-profile-root /path/to/root

python3 .agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py \
  --host 10.0.0.1 --search-root /path/to/roots
```
