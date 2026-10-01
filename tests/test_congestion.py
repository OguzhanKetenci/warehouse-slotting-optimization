"""
Unit tests for Module 8a (src/08_congestion_simulation.py): routes walked in the simulation,
aisle blocking, and reproducibility.

Run from the repo root:  python -m pytest tests/

Layout as in tests/test_routing.py: aisle length 50 m, aisle j (0-based) at x = 1.5 + 3j,
depot at x = 0, y = 0; walking speed 1 m/s, so metres = seconds.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("congestion", ROOT / "src" / "08_congestion_simulation.py")
m8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m8)
sr, m7 = m8.sr, m8.m7
HAVE_DATA = (m8.PROC / "clean_lines.csv").exists() and (m8.PROC / "optimal_routing_orders.csv.gz").exists()


def random_order(rng, max_picks=25):
    n = int(rng.integers(1, max_picks + 1))
    return rng.integers(0, 40, n), rng.integers(0, 50, n) + 0.5


def optimal_route(aisles, ys):
    length, la, ly, lc, tour = m8.optimal_tour(np.asarray(aisles), np.asarray(ys, dtype=float))
    return length, m8.tour_route(la, ly, lc, tour)


# --- Routes walked in the simulation ------------------------------------------------------------

def test_route_walks_equal_module2_and_module7_distances_random_orders():
    rng = np.random.default_rng(21)
    for _ in range(200):
        aisles, ys = random_order(rng)
        assert m8.route_walk_m(m8.s_shape_route(aisles, ys)) == sr.s_shape_distance(aisles, ys)
        length, route = optimal_route(aisles, ys)
        assert m8.route_walk_m(route) == pytest.approx(length, abs=1e-9)
        assert sum(v[3] for v in route[0]) == len(aisles)        # every SKU picked exactly once


def test_hand_example_routes():
    # Module 7's hand example: aisle 1 pick at 49 m, aisle 2 pick at 5 m. Both routes go up
    # aisle 1 (enter front, leave back) and down aisle 2: cross 1.5, aisle 50, cross 3, aisle 50,
    # cross 4.5 back to the depot = 109 m.
    s_visits, s_final = m8.s_shape_route([0, 1], [49.0, 5.0])
    assert s_visits == [(1.5, 0, 50.0, 1), (3.0, 1, 50.0, 1)] and s_final == 4.5
    length, (o_visits, o_final) = optimal_route([0, 1], [49.0, 5.0])
    assert length == 109.0 and m8.route_walk_m((o_visits, o_final)) == 109.0
    assert sorted(v[1] for v in o_visits) == [0, 1]


# --- Simulation ---------------------------------------------------------------------------------

def synthetic_day(rng, n_orders, start=8 * 3600, spread=3600):
    orders = [random_order(rng) for _ in range(n_orders)]
    routes = m8.timed_routes([m8.s_shape_route(a, y) for a, y in orders], m8.PICK_TIME_S)
    arrivals = np.sort(rng.uniform(start, start + spread, n_orders))
    return arrivals, np.arange(n_orders), routes


def test_one_picker_unlimited_aisles_no_wait_and_exact_walk_time():
    rng = np.random.default_rng(22)
    arrivals, idx, routes = synthetic_day(rng, 40)
    start, end, wait, _, _ = m8.simulate_day(arrivals, idx, routes, n_pickers=1, capacity=None)
    assert (wait == 0).all()
    walk = np.array([r[2] for r in routes])
    picks = np.array([r[3] for r in routes])
    assert np.allclose(end - start - m8.ORDER_HANDLING_S - picks, walk, atol=1e-6)


def test_one_picker_capacity_one_never_waits():
    rng = np.random.default_rng(23)
    arrivals, idx, routes = synthetic_day(rng, 40)
    _, _, wait, _, _ = m8.simulate_day(arrivals, idx, routes, n_pickers=1, capacity=1)
    assert (wait == 0).all()


def test_very_large_capacity_means_no_wait():
    rng = np.random.default_rng(24)
    arrivals, idx, routes = synthetic_day(rng, 300, spread=600)
    _, _, wait, _, aisle_wait = m8.simulate_day(arrivals, idx, routes, n_pickers=30, capacity=10**6)
    assert (wait == 0).all() and (aisle_wait == 0).all()
    _, _, wait1, _, _ = m8.simulate_day(arrivals, idx, routes, n_pickers=30, capacity=1)
    assert wait1.sum() > 0                                         # sanity: blocking does occur


def test_hand_example_blocking():
    # Two identical single-aisle orders arrive together at 08:00, two pickers, capacity 1.
    # Order: one pick at 20.5 m in aisle 0 -> S-shape (odd) = enter front, return: 41 m in aisle.
    # Both pickers: 60 s handling, 1.5 s cross. Picker 1 holds the aisle for 41 + 10 = 51 s;
    # picker 2 waits exactly 51 s, then needs 51 s more, then 1.5 s back.
    routes = m8.timed_routes([m8.s_shape_route([0], [20.5])], 10.0)
    arrivals, idx = np.array([8 * 3600.0, 8 * 3600.0]), np.array([0, 0])
    start, end, wait, _, _ = m8.simulate_day(arrivals, idx, routes, n_pickers=2, capacity=1)
    assert wait.tolist() == [0.0, 51.0]
    assert (end - 8 * 3600).tolist() == [60 + 1.5 + 51 + 1.5, 60 + 1.5 + 51 + 51 + 1.5]
    # with room for two pickers nobody waits
    _, _, wait2, _, _ = m8.simulate_day(arrivals, idx, routes, n_pickers=2, capacity=2)
    assert wait2.tolist() == [0.0, 0.0]


def test_overtime_and_queue_before_shift():
    # An order at 06:00 waits for the shift to start (08:00); one arriving at 17:59:50 finishes
    # after 18:00 and its picker's time after 18:00 is overtime.
    routes = m8.timed_routes([m8.s_shape_route([0], [20.5])], 10.0)
    arrivals = np.array([6 * 3600.0, 18 * 3600.0 - 10])
    start, end, _, last, _ = m8.simulate_day(arrivals, np.array([0, 0]), routes, n_pickers=1, capacity=1)
    assert start[0] == 8 * 3600
    assert last[0] - 18 * 3600 == pytest.approx(-10 + 60 + 1.5 + 51 + 1.5)


def test_same_seed_same_result():
    rng = np.random.default_rng(25)
    arrivals, idx, routes = synthetic_day(rng, 200, spread=1800)
    a = m8.simulate_day(arrivals, idx, routes, n_pickers=6, capacity=1)
    b = m8.simulate_day(arrivals, idx, routes, n_pickers=6, capacity=1)
    for x, y in zip(a, b):
        assert np.array_equal(x, y)
    orders = pd.DataFrame({"arrival": pd.to_datetime(["2011-01-04 09:00", "2011-01-04 17:50", "2011-01-05 00:10"]),
                           "n_skus": [1, 2, 3]})
    r1, r2 = m8.replicate(orders, 5), m8.replicate(orders, 5)
    pd.testing.assert_frame_equal(r1, r2)
    assert (r1.groupby("base")["date"].nunique() == 1).all()       # copies stay on the same date
    assert len(r1) == 15 and ((r1["t"] >= 0) & (r1["t"] < 86_400)).all()


# --- Real data (needs Modules 1, 2 and 7 outputs and the tour cache) ---------------------------

@pytest.mark.skipif(not HAVE_DATA, reason="processed data not found")
def test_real_orders_walk_equals_module_distances():
    layout, visit_order, visit_sku, orders, runs = m8.layout_runs_and_orders()
    m7_orders = pd.read_csv(m8.PROC / "optimal_routing_orders.csv.gz")
    tours = None
    if m8.TOUR_CACHE.exists():
        tours = pd.read_pickle(m8.TOUR_CACHE)["tours"]
    for scenario, ranks in runs:
        a, y = m7.pick_coordinates(ranks, layout, visit_sku)
        orders_xy = m7.split_orders(visit_order, a, y)
        ref = m7_orders[(m7_orders["scenario"] == scenario)
                        & (m7_orders["seed"].isna() | (m7_orders["seed"] == m8.LAYOUT_SEED))].sort_values("order")
        s_walk = np.array([m8.route_walk_m(m8.s_shape_route(oa, oy)) for oa, oy in orders_xy])
        assert np.array_equal(s_walk, ref["S-shape"].to_numpy())
        if tours is not None:
            o_walk = np.array([m8.route_walk_m(m8.tour_route(r[1], r[2], r[3], r[4])) for r in tours[scenario]])
            assert np.allclose(o_walk, [r[0] for r in tours[scenario]], atol=1e-6)
    # one real day, one picker, unlimited aisles: walk time per order = route length / 1 m/s
    routes = m8.timed_routes([m8.s_shape_route(oa, oy) for oa, oy in orders_xy], m8.PICK_TIME_S)
    day = m8.replicate(orders, 1)
    day = day[day["date"] == day["date"].iloc[0]]
    s, e, w, _, _ = m8.simulate_day(day["t"].to_numpy(), day["base"].to_numpy(), routes, 1, None)
    walk = np.array([routes[b][2] for b in day["base"]])
    picks = np.array([routes[b][3] for b in day["base"]])
    assert (w == 0).all() and np.allclose(e - s - m8.ORDER_HANDLING_S - picks, walk, atol=1e-6)
