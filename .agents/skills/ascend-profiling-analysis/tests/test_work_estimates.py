"""Regression tests for the shape-derived byte/FLOP estimates in
``ascend_profile.common`` (substring-matched factor tables).
"""
from __future__ import annotations

import conftest  # noqa: F401

from ascend_profile.common import dtype_bytes, estimate_vector_flops


def test_vector_flops_prefers_specific_key_over_insertion_order() -> None:
    # "AddRmsNorm" contains both "ADD" (1.0/elem) and "RMSNORM" (5.0/elem);
    # the longer, more specific key must win regardless of dict order.
    flops = estimate_vector_flops("AddRmsNorm", "add_rms_norm", [[4, 128]])
    assert flops == 4 * 128 * 5.0


def test_vector_flops_simple_add_still_matches_add() -> None:
    flops = estimate_vector_flops("Add", "add", [[4, 128]])
    assert flops == 4 * 128 * 1.0


def test_dtype_bytes_resolves_underscored_float8_spellings() -> None:
    # dtype_bytes() strips underscores before lookup, so the table keys are
    # normalized spellings; both e4m3fn and e5m2 are 1-byte dtypes.
    assert dtype_bytes("FLOAT8_E4M3FN") == 1.0
    assert dtype_bytes("float8_e5m2") == 1.0
