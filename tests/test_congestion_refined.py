"""
Unit tests for Module 8b (src/08b_congestion_refined.py): segment-level aisles, step-aside
deadlock rule, skip-and-return, Spread velocity slotting.

Run from the repo root:  python -m pytest tests/

Layout as in tests/test_routing.py: aisle length 50 m (10 segments of 5 m), aisle j (0-based)
at x = 1.5 + 3j, depot at x = 0; 1 m/s, so metres = seconds.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("congestion_refined", ROOT / "src" / "08b_congestion_refined.py")
m8b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m8b)
m8, sr, m5, m7 = m8b.m8, m8b.sr, m8b.m5, m8b.m7
HAVE_DATA = m8.TOUR_CACHE.exists() and (m8.PROC / "optimal_routing_orders.csv.gz").exists()
DAY0 = 8 * 3600.0


def random_order(rng, max_picks=25):
    n = int(rng.integers(1, max_picks + 1))
    return rng.integers(0, 40, n), rng.integers(0, 50, n) + 0.5


def optimal_visits(aisles, ys):
    length, la, ly, lc, tour = m8.optimal_tour(np.asarray(aisles), np.asarray(ys, dtype=float))
    return length, m8b.tour_visits(la, ly, lc, tour)


def run(orders_visits, arrivals, n_pickers, capacity, policy="wait"):
    routes = m8b.prepare_routes(orders_visits)
    return m8b.simulate_day(np.asarray(arrivals, dtype=float), np.arange(len(arrivals)), routes,
                            n_pickers, capacity, policy)


# --- Routes and timelines -----------------------------------------------------------------------

def test_route_walks_equal_module2_and_module7_random_orders():
    rng = np.random.default_rng(31)
    for _ in range(200):
        aisles, ys = random_order(rng)
        assert m8b.visits_walk_m(m8b.s_shape_visits(aisles, ys)) == sr.s_shape_distance(aisles, ys)
        length, visits = optimal_visits(aisles, ys)
        assert m8b.visits_walk_m(visits) == pytest.approx(length, abs=1e-9)
        for visits_ in (m8b.s_shape_visits(aisles, ys), visits):
            assert sum(c for v in visits_ for _, c in v[4]) == len(aisles)


def test_timeline_hand_example():
    # Traverse from the front with one pick at 12.5 m (2 SKUs): segments 0, 1 (5 s each), then
    # segment 2 holds 2.5 s walk + 20 s picking + 2.5 s walk, then segments 3..9 at 5 s each.
    tl, walk = m8b.timeline([0.0, 12.5, 50.0], [0, 2, 0], pick_time_s=10.0)
    assert walk == 50.0
    assert tl == ((0, 5.0), (1, 5.0), (2, 25.0)) + tuple((s, 5.0) for s in range(3, 10))
    # return trip from the front to 7.5 m: segment 0 (5 s), segment 1 (2.5 in + 10 pick + 2.5 out), segment 0 (5 s)
    tl, walk = m8b.timeline([0.0, 7.5, 0.0], [0, 1, 0], pick_time_s=10.0)
    assert walk == 15.0 and tl == ((0, 5.0), (1, 15.0), (0, 5.0))


def test_spread_k1_equals_aisle_based_velocity_and_round_robin():
    layout = sr.build_layout()
    rng = np.random.default_rng(32)
    pick_lines = rng.integers(0, 1000, 3791)
    assert np.array_equal(m8b.slot_spread_velocity(pick_lines, layout, 1),
                          m5.slot_aisle_based_velocity(pick_lines, layout))
    ranks = m8b.slot_spread_velocity(np.arange(3791)[::-1].copy(), layout, 5)   # SKU 0 fastest
    slots = layout.iloc[ranks[:12]]
    # first 5 SKUs: position 0, left rack, aisles 0..4; next 5: position 0, right rack, aisles 0..4
    assert slots["aisle"].tolist()[:10] == [0, 1, 2, 3, 4] * 2
    assert slots["pos"].tolist()[:10] == [0] * 10 and slots["side"].tolist()[:10] == [0] * 5 + [1] * 5
    assert slots["pos"].tolist()[10:12] == [1, 1]
    assert len(set(m8b.slot_spread_velocity(pick_lines, layout, 20))) == 3791


# --- Simulation ---------------------------------------------------------------------------------

def test_one_picker_no_wait_and_walk_equals_route_length():
    rng = np.random.default_rng(33)
    orders = [random_order(rng) for _ in range(40)]
    for build in (lambda a, y: m8b.s_shape_visits(a, y), lambda a, y: optimal_visits(a, y)[1]):
        visits = [build(a, y) for a, y in orders]
        arrivals = np.sort(rng.uniform(DAY0, DAY0 + 3600, len(orders)))
        for cap, policy in ((1, "wait"), (1, "skip"), (None, "wait")):
            out, _, _ = run(visits, arrivals, 1, cap, policy)
            assert (out["wait"] == 0).all() and (out["skips"] == 0).all()
            assert np.allclose(out["walk"], [m8b.visits_walk_m(v) for v in visits], atol=1e-6)
            picks = np.array([sum(c for v in vv for _, c in v[4]) * m8b.PICK_TIME_S for vv in visits])
            assert np.allclose(out["end"] - out["start"] - m8b.ORDER_HANDLING_S - picks, out["walk"], atol=1e-6)


def test_very_large_segment_capacity_means_no_wait():
    rng = np.random.default_rng(34)
    orders = [m8b.s_shape_visits(*random_order(rng)) for _ in range(300)]
    arrivals = np.sort(rng.uniform(DAY0, DAY0 + 600, 300))
    out, _, _ = run(orders, arrivals, 30, 10**6)
    assert (out["wait"] == 0).all()
    out1, _, _ = run(orders, arrivals, 30, 1)
    assert out1["wait"].sum() > 0                                  # sanity: blocking does occur


def test_hand_example_blocking_same_and_different_segment():
    # Order A: aisle 0, one pick at 20.5 m (segment 4) -> return trip from the front.
    #   Timeline: seg 0..3 at 5 s each, seg 4: 0.5 + 10 + 0.5 = 11 s, seg 3..0 at 5 s each.
    # Order B (same aisle): one pick at 2.5 m (segment 0): seg 0: 2.5 + 10 + 2.5 = 15 s.
    # Both start at 08:00 (+60 s handling + 1.5 s cross aisle), narrow aisle.
    a = m8b.s_shape_visits([0], [20.5])
    b = m8b.s_shape_visits([0], [2.5])
    t_in = DAY0 + 60 + 1.5
    # Two copies of A: picker 2 waits for segment 0 while picker 1 is in it (5 s), then follows
    # one segment behind; at the turn picker 1 comes back into segment 3 while picker 2 waits
    # for segment 4 (picker 1 is there for 11 s) -> picker 2 steps aside and they pass.
    out, _, _ = run([a, a], [DAY0, DAY0], 2, 1)
    assert out["wait"][0] == 0.0
    assert out["wait"][1] == pytest.approx(5.0 + 6.0)     # 5 s at the entry + 6 s at segment 4
    assert np.isfinite(out["end"]).all() and (out["end"] > 0).all()
    # A and B: B's whole stay is in segment 0. Picker 1 (A) enters first (FIFO tie by order),
    # picker 2 (B) waits 5 s for segment 0, then picks there for 15 s; picker 1 returns into
    # segment 0 at t_in + 5*4 + 11 + 5*3 = t_in + 46 > t_in + 5 + 15, so it never waits.
    out, _, _ = run([a, b], [DAY0, DAY0], 2, 1)
    assert out["wait"].tolist() == [0.0, 5.0]
    assert out["end"][1] == pytest.approx(t_in + 5 + 15 + 1.5)
    # different segments never block: B in segment 0 after A has moved on (A starts 10 s earlier)
    out, _, _ = run([a, b], [DAY0, DAY0 + 10], 2, 1)
    assert out["wait"].tolist() == [0.0, 0.0]
    # wide aisle: nobody waits
    out, _, _ = run([a, a], [DAY0, DAY0], 2, 2)
    assert out["wait"].tolist() == [0.0, 0.0]


def test_no_deadlock_on_a_crowded_day():
    # 40 pickers, 600 orders in 10 minutes, narrow aisles, both policies: the simulation must
    # finish (SimPy's run() returns when no events are left) with every order completed.
    rng = np.random.default_rng(35)
    orders = [m8b.s_shape_visits(*random_order(rng)) for _ in range(300)]
    orders += [optimal_visits(*random_order(rng))[1] for _ in range(300)]
    arrivals = np.sort(rng.uniform(DAY0, DAY0 + 600, 600))
    for policy in ("wait", "skip"):
        out, _, _ = run(orders, arrivals, 40, 1, policy)
        assert (out["end"] > out["start"]).all()
        assert (out["wait"] > 0).any()


def test_skip_and_return_hand_example():
    # Picker 1 (order at 08:00): aisle 1 only, pick at 2.5 m -> in segment 0 of aisle 1 (x = 4.5)
    # from 08:00 + 60 + 4.5 = +64.5 s to +79.5 s. Picker 2 (order at +10 s): aisles 1 and 3, picks
    # at 2.5 m (two aisles -> S-shape traverses both). Picker 2 decides at +70 s, before walking:
    # aisle 1's front segment is occupied.
    #  - wait: arrives at +74.5 s and waits until picker 1 leaves at +79.5 s -> 5 s.
    #  - skip: goes to aisle 3 first (entered from the front, mirror of the planned back entry),
    #    then back along the back cross aisle into aisle 1 from the back: no wait, one skip.
    #    Walk: 10.5 + 50 + 6 + 50 + 4.5 = 121 m (same length as planned here).
    p1 = m8b.s_shape_visits([1], [2.5])
    p2 = m8b.s_shape_visits([1, 3], [2.5, 2.5])
    out, _, _ = run([p1, p2], [DAY0, DAY0 + 10], 2, 1, "wait")
    assert out["wait"][1] == pytest.approx(5.0) and out["skips"][1] == 0
    out_s, _, _ = run([p1, p2], [DAY0, DAY0 + 10], 2, 1, "skip")
    assert out_s["wait"][1] == 0 and out_s["skips"][1] == 1
    assert out_s["walk"][1] == pytest.approx(10.5 + 50 + 6 + 50 + 4.5)
    # decided too early (order at +1 s, decision at +61 s): segment still free -> no skip, waits 14 s
    out_e, _, _ = run([p1, p2], [DAY0, DAY0 + 1], 2, 1, "skip")
    assert out_e["skips"][1] == 0 and out_e["wait"][1] == pytest.approx(14.0)


def test_same_seed_same_result():
    rng = np.random.default_rng(36)
    orders = [m8b.s_shape_visits(*random_order(rng)) for _ in range(200)]
    arrivals = np.sort(rng.uniform(DAY0, DAY0 + 1800, 200))
    for policy in ("wait", "skip"):
        a, _, _ = run(orders, arrivals, 8, 1, policy)
        b, _, _ = run(orders, arrivals, 8, 1, policy)
        for k in a:
            assert np.array_equal(a[k], b[k])


# --- Real data ----------------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_DATA, reason="processed data / Module 8a tour cache not found")
def test_real_orders_walk_equals_module7():
    m7_orders = pd.read_csv(m8.PROC / "optimal_routing_orders.csv.gz")
    for scenario in (sr.SCENARIO_RANDOM, sr.SCENARIO_VELOCITY):
        ref = m7_orders[(m7_orders["scenario"] == scenario)
                        & (m7_orders["seed"].isna() | (m7_orders["seed"] == 0))].sort_values("order")
        for route, col in (("S-shape", "S-shape"), ("Optimal", "Optimal")):
            routes = m8b.routes_for(scenario, route)
            walk = np.array([sum(p[2] for p in r) + sum(abs(p[0][1] - (r[k - 1][0][1] if k else 0.0))
                                                         for k, p in enumerate(r)) + r[-1][0][1] for r in routes])
            assert np.allclose(walk, ref[col].to_numpy(), atol=1e-6), (scenario, route)
