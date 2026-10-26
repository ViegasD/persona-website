"""Unit tests for the composite-cache hashing function used by the worker."""

from __future__ import annotations

from app.workers.batch_runner import composite_cache_key


def test_cache_key_preserves_left_to_right_order() -> None:
    assert composite_cache_key([1, 2, 3], "Lara", "aniversario") != composite_cache_key(
        [3, 2, 1], "Lara", "aniversario"
    )


def test_cache_key_is_case_insensitive() -> None:
    assert composite_cache_key([1], "Lara", "aniversario") == composite_cache_key(
        [1], "lara", "ANIVERSARIO"
    )


def test_cache_key_changes_with_inputs() -> None:
    base = composite_cache_key([1, 2], "lara", "aniversario")
    assert base != composite_cache_key([1, 2, 3], "lara", "aniversario")
    assert base != composite_cache_key([1, 2], "joao", "aniversario")
    assert base != composite_cache_key([1, 2], "lara", "natal")


def test_reference_revision_and_model_invalidate_cache():
    base = composite_cache_key(["a"], "lara", "natal", reference_hashes=["old"], model="old")
    assert base != composite_cache_key(["a"], "lara", "natal", reference_hashes=["new"], model="old")
    assert base != composite_cache_key(["a"], "lara", "natal", reference_hashes=["old"], model="new")
