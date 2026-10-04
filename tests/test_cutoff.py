"""
Unit tests for Module 8c (src/08c_cutoff_service.py): order cut-off with next-day carry-over.

Run from the repo root:  python -m pytest tests/
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cutoff", ROOT / "src" / "08c_cutoff_service.py")
m8c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m8c)
m8b = m8c.m8b
H = 3600.0


def random_order(rng, max_picks=20):
    n = int(rng.integers(1, max_picks + 1))
    return rng.integers(0, 40, n), rng.integers(0, 50, n) + 0.5


def synthetic(rng, n_orders, days=3, start=7 * H, end=20 * H):
    routes = m8b.prepare_routes([m8b.s_shape_visits(*random_order(rng)) for _ in range(n_orders)])
    dates = pd.to_datetime("2011-03-01") + pd.to_timedelta(rng.integers(0, days, n_orders), unit="D")
    arr = pd.DataFrame({"base": np.arange(n_orders), "date": dates, "t": rng.uniform(start, end, n_orders)})
    return routes, arr.sort_values(["date", "t"], kind="mergesort").reset_index(drop=True)


def test_no_deferral_flags_reproduce_plain_fifo():
    # deferrable all 0 (cut-off 24:00) -> the priority-queue path gives exactly the plain FIFO result
    rng = np.random.default_rng(41)
    routes, arr = synthetic(rng, 300, days=1, start=8 * H, end=9 * H)
    t, base = arr["t"].to_numpy(), arr["base"].to_numpy()
    for policy in ("wait", "skip"):
        a, la, wa = m8b.simulate_day(t, base, routes, 6, 1, policy)
        b, lb, wb = m8b.simulate_day(t, base, routes, 6, 1, policy, deferrable=np.zeros(len(t), int), defer_at_s=18 * H)
        for k in a:
            assert np.array_equal(a[k], b[k])
        assert np.array_equal(la, lb) and np.array_equal(wa, wb) and (b["deferred"] == 0).all()


def test_cutoff_24_year_equals_independent_days():
    rng = np.random.default_rng(42)
    routes, arr = synthetic(rng, 200)
    rec, post, ot_work, _, left = m8c.simulate_year(arr, routes, 4, 1, "wait", 24)
    assert (post == 0).all() and left == 0 and (rec["days_late"] == 0).all()
    for _, day in arr.groupby("date"):
        out, _, _ = m8b.simulate_day(day["t"].to_numpy(), day["base"].to_numpy(), routes, 4, 1, "wait")
        assert np.array_equal(rec["end"][day.index.to_numpy()], out["end"])


def test_hand_example_cutoff_and_carry_over():
    # One picker. Day 1: order A at 17:30 (after a 17:00 cut-off) and nothing else waiting -> picked
    # the same day. Order B at 18:30 (after cut-off and after the shift) -> not picked on day 1,
    # first in the queue on day 2 at 08:00, before day 2's order C that arrived at 07:00.
    routes = m8b.prepare_routes([m8b.s_shape_visits([0], [20.5])])   # 60 s + 1.5 + (41 + 10) + 1.5 = 114 s
    arr = pd.DataFrame({"base": [0, 0, 0],
                        "date": pd.to_datetime(["2011-03-01", "2011-03-01", "2011-03-02"]),
                        "t": [17.5 * H, 18.5 * H, 7 * H]})
    rec, post, ot_work, _, left = m8c.simulate_year(arr, routes, 1, 1, "wait", 17)
    assert post.tolist() == [1, 1, 0] and left == 0
    assert rec["days_late"].tolist() == [0, 1, 0]
    assert rec["start"][0] == 17.5 * H and rec["end"][0] == pytest.approx(17.5 * H + 114)
    assert rec["start"][1] == 8 * H                          # carried order first on day 2
    assert rec["start"][2] == pytest.approx(8 * H + 114)     # day 2's own order after it
    assert ot_work.tolist() == [0.0, 0.0]


def test_pre_cutoff_orders_have_priority_over_post_cutoff():
    # Cut-off 17:01. One picker starts a long order (two full aisles, about 3 min) at 17:00. While
    # they are busy, a pre-cut-off order (17:00:50) and a post-cut-off order (17:01:10) both queue up;
    # the pre-cut-off one is served first.
    routes = m8b.prepare_routes([m8b.s_shape_visits([0, 1], [49.5, 49.5])])   # long order (~2.3 min)
    arr = pd.DataFrame({"base": [0, 0, 0], "date": pd.to_datetime(["2011-03-01"] * 3),
                        "t": [17 * H, 17 * H + 50, 17 * H + 70]})
    cutoff_h = (17 * H + 60) / H                                               # 17:01
    rec, post, _, _, _ = m8c.simulate_year(arr, routes, 1, 1, "wait", cutoff_h)
    assert post.tolist() == [0, 0, 1]
    assert rec["start"][1] < rec["start"][2]


def test_same_seed_same_result():
    rng = np.random.default_rng(43)
    routes, arr = synthetic(rng, 300)
    a = m8c.simulate_year(arr, routes, 3, 1, "skip", 16)
    b = m8c.simulate_year(arr, routes, 3, 1, "skip", 16)
    for k in a[0]:
        assert np.array_equal(a[0][k], b[0][k], equal_nan=True)
    assert np.array_equal(a[2], b[2]) and a[4] == b[4]
