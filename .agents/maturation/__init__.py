"""Deterministic-core maturation harness.

Runs declared deterministic remote operations repeatedly across several
endpoints, in adversarial shapes, and reports pass *rates* with per-failure
layer attribution instead of a single boolean verdict.

The harness uses the installed ``vaws-remote-dev`` package and invokes it
in-process (``remote_dev.mcp.tools.call_tool``) or via
``python -m remote_dev``. It never changes the substrate.
All evidence lands under untracked ``.vaws-local/``.
"""
