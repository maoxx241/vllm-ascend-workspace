---
name: machine-management
description: Store the project username for the persistent vaws user container. Bootstrap, repair, and deletion belong to vaws-coordinator.
---

# Machine username (project config)

Each host has one long-lived container `vaws-<user>` (example `vaws-maoxx241`).
This skill only records the local username document. Coordinator prepares the
container and environments.

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py --machine-username maoxx241
python3 .agents/skills/machine-management/scripts/machine_verify.py
python -m vaws_coordinator provision --host <host> --image <image> --user <user>
```

Do not call deleted `machine_repair.py` or `machine_remove.py`. Image
selection (`local-latest`, `rc`, `main`, `stable`, or a concrete image) is an
argument to the published provision command.
