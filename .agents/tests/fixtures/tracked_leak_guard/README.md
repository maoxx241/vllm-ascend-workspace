# Tracked-leak-guard fixtures

`dirty.txt` and `clean.txt` are the corpus for `test_tracked_leak_scan.py`.
`dirty.txt` deliberately contains one match per detection category so a
regression in any rule fails a test instead of passing silently.

Every value here is reserved by a standard and cannot name a real host, person,
or credential:

- `192.168.x` / `10.x` — RFC 1918 private use, not routable on the internet
- `fd00::/8` — RFC 4193 unique-local address space
- `00:00:5e:00:53:xx` is the RFC 7042 documentation MAC range, so `dirty.txt`
  uses a different locally-administered value to trip the MAC rule instead
- `example.invalid` / `.invalid` — RFC 2606 reserved TLD that never resolves
- fabricated home directories, employee-shaped ids, and credential strings that
  authenticate nothing

The tracked-tree scan does **not** read these files. They are covered by the
`leak-guard-test-fixtures` scoped exclusion in
`.agents/leak-guard/allowlist.yaml`; the tests reach them by scanning this
directory explicitly with a policy that has no exclusion.
