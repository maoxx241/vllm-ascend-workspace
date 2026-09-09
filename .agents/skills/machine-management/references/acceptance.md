# Acceptance

- This skill records the project username for `vaws-<user>`.
- Verify does not take `--machine` and does not claim container SSH proof.
- `machine_repair.py` and `machine_remove.py` are deleted, not refusal stubs.
- Bootstrap, image selection, and container deletion are coordinator-owned
  (`python -m vaws_coordinator provision --host ... --image ... --user ...`).
