# Property tests

Status: current

The local property suites check deterministic contracts with seeded inputs.
Run the affected suites through the ordinary local runner:

```text
uv run --no-project python .agents/scripts/local_tests.py .agents/tests/test_property_validate.py .agents/tests/test_property_run_manifest.py
```

- Shared validation checks identifier, environment-name, device-list and child-path rules.
- Run Manifest tests exercise the installed coordinator implementation against its schema, including round trips and invalid evidence.

The helper `.agents/tests/test_property_support.py` derives each case's seed from
the suite seed and case index. Failure output includes both. Set
`VAWS_PROPTEST_SEED` to repeat or vary a suite and `VAWS_PROPTEST_SCALE` to adjust
the bounded case count. Normal iteration uses the defaults.

Remote transport, remote path/patch/artifact properties and resource arbitration
belong to their installed runtime repositories. The consumer does not duplicate
those suites or provide a VAWS endpoint resolver. These tests provide local
control-plane evidence; they do not establish network-filesystem locking,
SSH recovery or Ascend hardware behavior.
