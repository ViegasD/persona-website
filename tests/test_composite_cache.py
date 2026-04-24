"""Unit tests for the composite-cache hashing function used by the worker."""

from __future__ import annotations

from app.workers.batch_runner import composite_cache_key


def test_cache_key_is_order_insensitive() -> None:
    assert composite_cache_key([1, 2, 3], "Lara", "aniversario") == composite_cache_key(
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
