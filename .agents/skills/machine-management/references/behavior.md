# Machine-management behavior

This skill stores the project username used for the persistent
`vaws-<user>` container. Bootstrap, repair, and deletion belong to
vaws-coordinator.

- `machine_add.py` records `--machine-username`.
- `machine_verify.py` reports the local username document.
- Container prepare uses `python -m vaws_coordinator provision --host ... --image ... --user ...`.

Deleted: `manage_machine.py`, `inventory.py`, `machine_repair.py`,
`machine_remove.py`, `--machine` as a bootstrap surface.
