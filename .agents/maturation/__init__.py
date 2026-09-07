"""Deterministic-core maturation harness.

Runs declared deterministic remote operations repeatedly across several
endpoints, in adversarial shapes, and reports pass *rates* with per-failure
layer attribution instead of a single boolean verdict.

The harness locates the external remote-dev checkout through
``vaws_remote_dev`` and invokes it in-process (``mcp.tools.call_tool``) or
via ``.agents/scripts/remote_dev.py tool``. It never changes the substrate.
All evidence lands under untracked ``.vaws-local/``.
"""
