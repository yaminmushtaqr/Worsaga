"""Tests for the bounded-concurrency + progress fan-out helper."""

import time

import pytest

from worsaga.concurrency import (
    DEFAULT_MAX_WORKERS,
    resolve_max_workers,
    run_parallel,
)


def test_run_parallel_preserves_input_order_despite_completion_order():
    # Later items finish first (reverse-proportional sleep), so completion
    # order is the opposite of input order; results must still come back in
    # input order.
    items = list(range(8))

    def _slow(i):
        time.sleep(0.02 * (len(items) - i))
        return i * 10

    results = run_parallel(items, _slow, label_fn=str)
    assert results == [i * 10 for i in items]


def test_run_parallel_progress_count_is_monotonic_and_complete():
    items = ["a", "b", "c", "d", "e"]
    seen = []

    def _work(item):
        time.sleep(0.01)
        return item.upper()

    def _progress(done, total, label):
        seen.append((done, total, label))

    results = run_parallel(
        items, _work, label_fn=lambda s: s, on_progress=_progress,
    )
    assert results == ["A", "B", "C", "D", "E"]
    # The counter is a monotonic 1..N and every total is N, even though the
    # labels may arrive in completion order.
    assert [done for done, _, _ in seen] == [1, 2, 3, 4, 5]
    assert {total for _, total, _ in seen} == {5}
    assert sorted(label for _, _, label in seen) == items


def test_run_parallel_empty_returns_empty_and_never_calls_progress():
    calls = []
    assert run_parallel([], lambda x: x, label_fn=str,
                         on_progress=lambda *a: calls.append(a)) == []
    assert calls == []


def test_run_parallel_propagates_worker_exception():
    def _boom(i):
        if i == 3:
            raise ValueError("item 3 failed")
        time.sleep(0.01)
        return i

    with pytest.raises(ValueError, match="item 3 failed"):
        run_parallel(list(range(6)), _boom, label_fn=str)


def test_run_parallel_single_item_runs_sequentially_with_progress():
    seen = []
    results = run_parallel(
        [42], lambda x: x + 1, label_fn=str,
        on_progress=lambda d, t, lbl: seen.append((d, t, lbl)),
    )
    assert results == [43]
    assert seen == [(1, 1, "42")]


def test_resolve_max_workers_never_exceeds_item_count():
    assert resolve_max_workers(1) == 1
    assert resolve_max_workers(3) == min(3, DEFAULT_MAX_WORKERS)
    assert resolve_max_workers(100) == DEFAULT_MAX_WORKERS


def test_resolve_max_workers_env_override(monkeypatch):
    monkeypatch.setenv("WORSAGA_CONCURRENCY", "3")
    assert resolve_max_workers(100) == 3
    # Clamped to a sane ceiling.
    monkeypatch.setenv("WORSAGA_CONCURRENCY", "9999")
    assert resolve_max_workers(100) == 32
    # Unparseable values fall back to the default.
    monkeypatch.setenv("WORSAGA_CONCURRENCY", "lots")
    assert resolve_max_workers(100) == DEFAULT_MAX_WORKERS


def test_run_parallel_max_workers_one_is_sequential():
    order = []

    def _work(i):
        order.append(i)
        return i

    results = run_parallel(
        [3, 1, 2], _work, label_fn=str, max_workers=1,
    )
    assert results == [3, 1, 2]
    # workers==1 takes the sequential branch: work runs in input order.
    assert order == [3, 1, 2]
