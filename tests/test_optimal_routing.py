"""
Unit tests for Module 7 (src/07_optimal_routing.py): warehouse graph distances, exact and
OR-Tools TSP, the Combined heuristic, and whole-dataset checks on the per-order output.

Run from the repo root:  python -m pytest tests/

Hand calculations use the default layout (as in tests/test_routing.py): aisle length 50 m,
aisle pitch 3 m, aisle j (0-based) at x = 1.5 + 3j, depot at x = 0, y = 0 (front cross aisle).
"""
import importlib.util
import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("optimal_routing", ROOT / "src" / "07_optimal_routing.py")
m7 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m7)
sr = m7.sr
ORDERS_FILE = ROOT / "data" / "processed" / "optimal_routing_orders.csv.gz"


def random_order(rng, max_picks=25, n_aisles=40):
    n = int(rng.integers(1, max_picks + 1))
    return rng.integers(0, n_aisles, n), rng.integers(0, 50, n) + 0.5


def brute_force_tsp(d):
    n = len(d) - 1
    return min(d[0, p[0]] + sum(d[a, b] for a, b in zip(p, p[1:])) + d[p[-1], 0]
               for p in itertools.permutations(range(1, n + 1)))


# --- Hand-calculated example: 2 aisles, pick at the back of aisle 1 and the front of aisle 2 ---

HAND_AISLES, HAND_YS = [0, 1], [49.0, 5.0]


def test_hand_example_distance_matrix():
    # depot -> (x=1.5, y=49): 1.5 + 49 = 50.5 | depot -> (x=4.5, y=5): 4.5 + 5 = 9.5
    # (1.5, 49) -> (4.5, 5): 3 across + min(via front 49 + 5 = 54, via back 1 + 45 = 46) = 49
    expected = np.array([[0.0, 50.5, 9.5], [50.5, 0.0, 49.0], [9.5, 49.0, 0.0]])
    assert np.array_equal(m7.distance_matrix(HAND_AISLES, HAND_YS), expected)
    assert np.array_equal(m7.graph_distance_matrix(HAND_AISLES, HAND_YS), expected)


def test_hand_example_optimal_route():
    # Optimal: depot -> front of aisle 1 (1.5) -> up to the pick (49) -> on to the back cross aisle (1)
    # -> across to aisle 2 (3) -> down to the pick (45) -> on to the front (5) -> back to depot (4.5)
    # = 109 m.  The alternative (enter and leave both aisles from the front) costs
    # 1.5 + 2*49 + 3 + 2*5 + 4.5 = 117 m.
    d = m7.distance_matrix(HAND_AISLES, HAND_YS)
    length, tour = m7.solve_tsp(d)
    assert length == 1.5 + 49 + 1 + 3 + 45 + 5 + 4.5 == 109.0
    assert tour in ([0, 1, 2, 0], [0, 2, 1, 0])
    assert m7.held_karp(d) == 109.0
    assert m7.solve_order(np.array(HAND_AISLES), np.array(HAND_YS))[0] == 109.0
    # The simple rules on the same order: S-shape and Largest gap traverse both aisles (109 m),
    # Return enters both from the front (117 m), Combined picks the better of the two (109 m).
    assert sr.s_shape_distance(HAND_AISLES, HAND_YS) == 109.0
    assert sr.largest_gap_distance(HAND_AISLES, HAND_YS) == 109.0
    assert sr.return_distance(HAND_AISLES, HAND_YS) == 117.0
    assert m7.combined_distance(HAND_AISLES, HAND_YS) == 109.0


# --- Distance matrix: closed form vs. Dijkstra on the explicit graph ---------------------------

def test_closed_form_matches_dijkstra_small_graph():
    # 3 aisles, two picks in the same aisle, two picks at the same location (facing racks).
    aisles, ys = [0, 0, 2, 2, 1], [10.5, 40.5, 25.5, 25.5, 0.5]
    assert np.array_equal(m7.distance_matrix(aisles, ys), m7.graph_distance_matrix(aisles, ys, n_aisles=3))


def test_closed_form_matches_dijkstra_random_orders():
    rng = np.random.default_rng(11)
    for _ in range(200):
        aisles, ys = random_order(rng)
        assert np.array_equal(m7.distance_matrix(aisles, ys), m7.graph_distance_matrix(aisles, ys))


# --- Exact TSP and OR-Tools --------------------------------------------------------------------

def test_held_karp_matches_brute_force():
    rng = np.random.default_rng(12)
    for _ in range(60):
        aisles, ys = random_order(rng, max_picks=7, n_aisles=6)
        d = m7.distance_matrix(aisles, ys)
        assert m7.held_karp(d) == pytest.approx(brute_force_tsp(d), abs=1e-9)


def test_ortools_matches_exact_on_small_orders():
    # Orders with <= 10 locations get guided local search (GLS_SOLUTIONS solutions); on these
    # random instances it must reach the exact Held-Karp optimum.
    rng = np.random.default_rng(13)
    for _ in range(100):
        aisles, ys = random_order(rng, max_picks=10)
        loc = np.unique(np.column_stack([aisles, ys]), axis=0)
        exact = m7.held_karp(m7.distance_matrix(loc[:, 0].astype(int), loc[:, 1]))
        assert m7.solve_order(aisles, ys)[0] == pytest.approx(exact, abs=1e-9)


def test_local_search_alone_can_get_stuck():
    # Real order 10745 under Random seed 0 (5 locations). Starting from the rule sequence
    # (418 m), OR-Tools' plain local search stops at 402 m, a true local optimum: no single swap,
    # 2-opt or relocate move improves it. The exact optimum (386 m) needs two moves in a row.
    # Guided local search escapes it - the reason Module 7 uses GLS for orders <= 40 locations.
    aisles, ys = [1, 3, 15, 32, 39], [45.5, 37.5, 40.5, 31.5, 45.5]
    d = m7.distance_matrix(aisles, ys)
    assert m7.held_karp(d) == brute_force_tsp(d) == 386.0
    assert m7.solve_tsp(d, initial=[1, 2, 3, 4, 5])[0] == 402.0
    assert m7.solve_tsp(d, initial=[1, 2, 3, 4, 5], gls_solutions=m7.GLS_SOLUTIONS)[0] == 386.0
    assert m7.solve_order(np.array(aisles), np.array(ys))[0] == 386.0


# --- Combined heuristic ------------------------------------------------------------------------

def test_combined_hand_example_beats_s_shape_and_return():
    # 4 aisles (x = 1.5, 4.5, 7.5, 10.5): picks at 45, 48, 3, 2 m. Horizontal = 2 * 10.5 = 21.
    # Combined: traverse aisle 1 (50, now at back) | aisle 2 from the back and return: 2*(50-48)=4
    #           | traverse aisle 3 (50, now at front) | aisle 4 from the front and return: 2*2=4
    #           vertical 108 -> 129 m.
    # S-shape: 4 aisles traversed = 200 -> 221 m.  Return: 2*(45+48+3+2) = 196 -> 217 m.
    aisles, ys = [0, 1, 2, 3], [45.0, 48.0, 3.0, 2.0]
    assert m7.combined_distance(aisles, ys) == 21 + 50 + 4 + 50 + 4 == 129.0
    assert sr.s_shape_distance(aisles, ys) == 221.0
    assert sr.return_distance(aisles, ys) == 217.0


def test_combined_vectorised_matches_scalar():
    rng = np.random.default_rng(14)
    orders = [random_order(rng) for _ in range(300)]
    order_idx = np.concatenate([np.full(len(a), i) for i, (a, _) in enumerate(orders)])
    aisle = np.concatenate([a for a, _ in orders])
    y = np.concatenate([yy for _, yy in orders])
    vec = m7.combined_distances(order_idx, aisle, y)
    assert np.array_equal(vec, [m7.combined_distance(a, yy) for a, yy in orders])


def test_combined_never_longer_than_s_shape_or_return_random():
    rng = np.random.default_rng(15)
    for _ in range(500):
        aisles, ys = random_order(rng)
        c = m7.combined_distance(aisles, ys)
        assert c <= sr.s_shape_distance(aisles, ys) + 1e-9
        assert c <= sr.return_distance(aisles, ys) + 1e-9


def test_rule_visiting_sequences_are_not_longer_than_the_rule():
    # Walking the Combined / Largest-gap visiting order along shortest paths can only be shorter
    # than (or equal to) the rule's own route length; this is what makes them valid warm starts.
    rng = np.random.default_rng(16)
    for _ in range(300):
        aisles, ys = random_order(rng)
        d = m7.distance_matrix(aisles, ys)
        for seq_fn, rule in ((m7.combined_sequence, m7.combined_distance),
                             (m7.largest_gap_sequence, sr.largest_gap_distance)):
            seq = seq_fn(aisles, ys)
            assert sorted(seq) == list(range(len(aisles)))
            assert m7.tour_length(d, [0] + [i + 1 for i in seq] + [0]) <= rule(aisles, ys) + 1e-9


# --- Whole dataset (needs `python src/07_optimal_routing.py` to have been run) ------------------

@pytest.fixture(scope="module")
def per_order():
    if not ORDERS_FILE.exists():
        pytest.skip(f"{ORDERS_FILE.name} not found - run src/07_optimal_routing.py first")
    return pd.read_csv(ORDERS_FILE)


def test_all_orders_optimal_not_longer_than_any_rule(per_order):
    for rule in ("S-shape", "Return", "Largest gap", "Combined"):
        excess = per_order["Optimal"] - per_order[rule]
        assert (excess <= 1e-9).all(), f"{int((excess > 1e-9).sum())} orders where Optimal > {rule}"


def test_all_orders_combined_not_longer_than_s_shape_or_return(per_order):
    best = np.minimum(per_order["S-shape"], per_order["Return"])
    assert (per_order["Combined"] <= best + 1e-9).all()
