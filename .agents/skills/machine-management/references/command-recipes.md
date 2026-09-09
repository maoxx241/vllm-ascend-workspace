# Command recipes

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py --machine-username maoxx241
python3 .agents/skills/machine-management/scripts/machine_verify.py
python -m vaws_coordinator provision --host <host> --image <image> --user <user>
```

`--image` is `local-latest`, `rc`, `main`, `stable`, or a concrete image
reference. Do not call deleted `inventory.py`, `manage_machine.py`,
`machine_repair.py`, or `machine_remove.py`.
