"""Deterministic-core maturation harness.

Runs declared deterministic remote operations repeatedly across several
endpoints, in adversarial shapes, and reports pass *rates* with per-failure
layer attribution instead of a single boolean verdict.

The harness exercises the existing ``.remote-dev`` tooling; it never changes
that tooling's behaviour. All evidence lands under untracked ``.vaws-local/``.
"""
