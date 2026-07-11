import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.services.traffic_splitter import _hash_to_unit_interval  # noqa: E402


def test_hash_is_deterministic():
    exp_id = uuid.uuid4()
    r1 = _hash_to_unit_interval(exp_id, "user-123")
    r2 = _hash_to_unit_interval(exp_id, "user-123")
    assert r1 == r2


def test_hash_is_uniform_in_unit_interval():
    exp_id = uuid.uuid4()
    for i in range(1000):
        r = _hash_to_unit_interval(exp_id, f"user-{i}")
        assert 0.0 <= r < 1.0


def test_hash_distribution_is_roughly_uniform():
    """Not a proof of uniformity, but a sanity check that ~5000 samples land
    roughly evenly across 10 buckets (each should get ~10% +/- a few pp)."""
    exp_id = uuid.uuid4()
    buckets = [0] * 10
    n = 5000
    for i in range(n):
        r = _hash_to_unit_interval(exp_id, f"user-{i}")
        buckets[min(int(r * 10), 9)] += 1
    for count in buckets:
        fraction = count / n
        assert 0.07 < fraction < 0.13, f"bucket fraction {fraction} not roughly uniform"


def test_different_experiments_give_different_assignments():
    """Same unit_id, different experiment_id, should generally land in
    different points of the interval (no cross-experiment correlation)."""
    exp_a, exp_b = uuid.uuid4(), uuid.uuid4()
    diffs = 0
    for i in range(200):
        ra = _hash_to_unit_interval(exp_a, f"user-{i}")
        rb = _hash_to_unit_interval(exp_b, f"user-{i}")
        if abs(ra - rb) > 0.01:
            diffs += 1
    assert diffs > 150  # overwhelming majority should differ meaningfully
