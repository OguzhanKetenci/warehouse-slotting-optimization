"""
Unit tests for the S-shape routing distance and slotting policies in src/02_slotting_routing.py.

Run from the repo root:  python -m pytest tests/    or    python tests/test_routing.py

Hand calculations use the default layout: aisle length 50 m, aisle pitch 3 m, first aisle at
x = 1.5 m, depot at x = 0, slot k (0-based) at y = k + 0.5 m. Aisle j (0-based) is at x = 1.5 + 3j.
"""
import importlib.util
from pathlib import Path

import numpy as np

SRC = Path(__file__).resolve().parents[1] / "src" / "02_slotting_routing.py"
spec = importlib.util.spec_from_file_location("slotting_routing", SRC)
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


def test_layout_constants_match_hand_calculations():
    assert (sr.AISLE_LENGTH_M, sr.AISLE_PITCH_M, sr.FIRST_AISLE_X_M) == (50.0, 3.0, 1.5)


def test_one_aisle_two_picks():
    # Picks: aisle 0 at slots 9 and 19 (y = 9.5 and 19.5). One aisle -> odd, enter and return.
    # depot -> aisle 0 front: 1.5 | up to y = 19.5: 19.5 | back down: 19.5 | back to depot: 1.5
    assert sr.s_shape_distance([0, 0], [9.5, 19.5]) == 1.5 + 19.5 + 19.5 + 1.5 == 42.0


def test_two_aisles():
    # Picks: aisle 0 at slot 4 (y = 4.5), aisle 2 at slot 30 (y = 30.5). Even -> both fully traversed.
    # depot -> x = 1.5: 1.5 | aisle 0 up: 50 | back cross aisle 1.5 -> 7.5: 6
    # | aisle 2 down: 50 | front cross aisle back to depot: 7.5
    assert sr.s_shape_distance([0, 2], [4.5, 30.5]) == 1.5 + 50 + 6 + 50 + 7.5 == 115.0


def test_three_aisles_last_aisle_half_entered():
    # Picks: aisle 1 slot 0 (y = 0.5), aisle 3 slot 49 (y = 49.5), aisle 4 slots 9 and 24 (y = 9.5, 24.5).
    # x: aisle 1 = 4.5, aisle 3 = 10.5, aisle 4 = 13.5. Three aisles -> last one half-entered.
    # depot -> 4.5: 4.5 | aisle 1 up: 50 | back cross aisle 4.5 -> 10.5: 6 | aisle 3 down: 50
    # | front cross aisle 10.5 -> 13.5: 3 | aisle 4 to y = 24.5 and back: 49 | back to depot: 13.5
    aisles, ys = [1, 3, 4, 4], [0.5, 49.5, 9.5, 24.5]
    assert sr.s_shape_distance(aisles, ys) == 4.5 + 50 + 6 + 50 + 3 + 49 + 13.5 == 176.0


def test_vectorised_matches_hand_calculations():
    # The three hand-computed orders above, evaluated together by the vectorised function.
    order = np.array([0, 0, 1, 1, 2, 2, 2, 2])
    aisle = np.array([0, 0, 0, 2, 1, 3, 4, 4])
    y = np.array([9.5, 19.5, 4.5, 30.5, 0.5, 49.5, 9.5, 24.5])
    assert sr.s_shape_distances(order, aisle, y).tolist() == [42.0, 115.0, 176.0]


# --- Return policy: 2 adjacent aisles (0 and 1, x = 1.5 and 4.5), one pick per aisle -----------

def test_two_aisles_shallow_picks_return_beats_s_shape():
    # Picks at y = 20 in both aisles. S-shape always fully traverses both aisles (n=2, even):
    #   horizontal 2*4.5=9 | vertical 50*2=100 | total 109 (independent of the picks' depth).
    # Return enters each aisle only to the pick and back:
    #   horizontal 9 | vertical 2*(20+20)=80 | total 89.
    assert sr.s_shape_distance([0, 1], [20.0, 20.0]) == 9 + 100 == 109.0
    assert sr.return_distance([0, 1], [20.0, 20.0]) == 9 + 80 == 89.0


def test_two_aisles_deep_picks_s_shape_beats_return():
    # Same two aisles, picks now at y = 40 (deep). S-shape is unchanged (still fully
    # traverses both aisles regardless of depth): total 109, same as the shallow case.
    # Return now walks in to y = 40 and back in each aisle: vertical 2*(40+40)=160, total 169.
    # So S-shape (109) is now shorter than Return (169) - the opposite of the shallow case.
    s_shape = sr.s_shape_distance([0, 1], [40.0, 40.0])
    ret = sr.return_distance([0, 1], [40.0, 40.0])
    assert s_shape == 109.0
    assert ret == 9 + 160 == 169.0
    assert s_shape < ret


# --- Largest gap: 3 aisles (0 = first, 1 = middle, 2 = last, x = 1.5/4.5/7.5) -------------------

def test_largest_gap_internal_gap_between_two_picks():
    # Middle aisle (1) has two picks at y = 10 and y = 40: gaps are 10 (front->10), 30 (10->40),
    # 10 (40->back) - the largest is the internal 30 m gap, so the aisle contributes
    # 2*(50-30) = 40 (enter front to y=10 and back: 20; enter back to y=40 and back: 20).
    # First (0) and last (2) aisles are each fully traversed once regardless of their own
    # pick depth: 50 + 50 = 100. Horizontal: 2*x(last aisle 2) = 2*7.5 = 15.
    # Total = 15 + 100 + 40 = 155 (cross-checked against an explicit waypoint route by hand).
    aisles = [0, 1, 1, 2]
    ys = [25.0, 10.0, 40.0, 15.0]   # first/last-aisle y is irrelevant (full traverse either way)
    assert sr.largest_gap_distance(aisles, ys) == 15 + 100 + 40 == 155.0


def test_largest_gap_boundary_gap_at_the_front():
    # Middle aisle (1) has a single pick at y = 45: gaps are 45 (front->45) and 5 (45->back).
    # The largest gap is the 45 m FRONT gap, so the picker skips the front entirely and enters
    # only from the back: 2*(50-45) = 10 (equivalently 2*5, straight in from the back and out).
    # First/last aisles: 50 + 50 = 100. Horizontal: 2*7.5 = 15. Total = 15 + 100 + 10 = 125.
    aisles = [0, 1, 2]
    ys = [25.0, 45.0, 15.0]
    assert sr.largest_gap_distance(aisles, ys) == 15 + 100 + 10 == 125.0


def test_largest_gap_equals_s_shape_when_there_is_no_middle_aisle():
    # With only 2 non-empty aisles there is no "middle" aisle to skip a gap in, so largest-gap
    # reduces exactly to S-shape. Reuses the fixture of test_two_aisles (expected 115.0).
    assert sr.largest_gap_distance([0, 2], [4.5, 30.5]) == sr.s_shape_distance([0, 2], [4.5, 30.5]) == 115.0


def _waypoint_route_length(aisles, ys):
    """Independent check: build the S-shape path as explicit waypoints and sum Manhattan legs."""
    x_of = lambda a: sr.FIRST_AISLE_X_M + sr.AISLE_PITCH_M * a
    farthest = {}
    for a, y in zip(aisles, ys):
        farthest[a] = max(farthest.get(a, 0.0), y)
    visited = sorted(farthest)
    path = [(0.0, 0.0)]
    for i, a in enumerate(visited):
        is_last = i == len(visited) - 1
        if i % 2 == 0:                                   # arrive at the front, walk up
            path.append((x_of(a), 0.0))
            if is_last:                                  # odd count: turn at the farthest pick
                path.append((x_of(a), farthest[a]))
                path.append((x_of(a), 0.0))
            else:
                path.append((x_of(a), sr.AISLE_LENGTH_M))
        else:                                            # arrive at the back, walk down
            path.append((x_of(a), sr.AISLE_LENGTH_M))
            path.append((x_of(a), 0.0))
    path.append((0.0, 0.0))
    return sum(abs(x2 - x1) + abs(y2 - y1) for (x1, y1), (x2, y2) in zip(path, path[1:]))


def test_formula_matches_waypoint_simulation_on_random_orders():
    rng = np.random.default_rng(123)
    orders = []
    for _ in range(300):
        n_picks = int(rng.integers(1, 30))
        aisles = rng.integers(0, sr.N_AISLES, n_picks)
        ys = (rng.integers(0, sr.SLOTS_PER_SIDE, n_picks) + 0.5) * sr.SLOT_WIDTH_M
        orders.append((aisles, ys))
    expected = [_waypoint_route_length(a, y) for a, y in orders]
    scalar = [sr.s_shape_distance(a, y) for a, y in orders]
    vector = sr.s_shape_distances(np.repeat(np.arange(len(orders)), [len(a) for a, _ in orders]),
                                  np.concatenate([a for a, _ in orders]),
                                  np.concatenate([y for _, y in orders]))
    assert np.allclose(scalar, expected)
    assert np.allclose(vector, expected)


def test_return_and_largest_gap_hand_calculations_match_vectorised():
    # The hand-computed Return orders above (shallow/deep picks), evaluated together by the
    # vectorised function (independent code path: pandas groupby vs. plain dict looping).
    ret_order = np.array([0, 0, 1, 1])
    ret_aisle = np.array([0, 1, 0, 1])
    ret_y = np.array([20.0, 20.0, 40.0, 40.0])
    assert sr.return_distances(ret_order, ret_aisle, ret_y).tolist() == [89.0, 169.0]

    # The hand-computed largest-gap orders above (internal gap, front-boundary gap, no middle
    # aisle), evaluated together by the vectorised function.
    lg_order = np.array([0, 0, 0, 0, 1, 1, 1, 2, 2])
    lg_aisle = np.array([0, 1, 1, 2, 0, 1, 2, 0, 2])
    lg_y = np.array([25.0, 10.0, 40.0, 15.0, 25.0, 45.0, 15.0, 4.5, 30.5])
    assert sr.largest_gap_distances(lg_order, lg_aisle, lg_y).tolist() == [155.0, 125.0, 115.0]


def _random_orders(seed: int, n_orders: int = 300):
    rng = np.random.default_rng(seed)
    orders = []
    for _ in range(n_orders):
        n_picks = int(rng.integers(1, 30))
        aisles = rng.integers(0, sr.N_AISLES, n_picks)
        ys = (rng.integers(0, sr.SLOTS_PER_SIDE, n_picks) + 0.5) * sr.SLOT_WIDTH_M
        orders.append((aisles, ys))
    return orders


def test_return_vectorised_matches_scalar_on_random_orders():
    orders = _random_orders(seed=7)
    scalar = [sr.return_distance(a, y) for a, y in orders]
    order_idx = np.repeat(np.arange(len(orders)), [len(a) for a, _ in orders])
    vector = sr.return_distances(order_idx, np.concatenate([a for a, _ in orders]),
                                 np.concatenate([y for _, y in orders]))
    assert np.allclose(scalar, vector)


def test_largest_gap_vectorised_matches_scalar_on_random_orders():
    orders = _random_orders(seed=11)
    scalar = [sr.largest_gap_distance(a, y) for a, y in orders]
    order_idx = np.repeat(np.arange(len(orders)), [len(a) for a, _ in orders])
    vector = sr.largest_gap_distances(order_idx, np.concatenate([a for a, _ in orders]),
                                      np.concatenate([y for _, y in orders]))
    assert np.allclose(scalar, vector)


def test_layout_is_sorted_by_walking_distance_and_large_enough():
    layout = sr.build_layout()
    assert len(layout) == sr.N_AISLES * sr.SIDES_PER_AISLE * sr.SLOTS_PER_SIDE
    assert layout["walk_m"].is_monotonic_increasing
    assert layout["walk_m"].iloc[0] == 1.5 + 0.5           # aisle 0, first slot
    assert not layout.duplicated(["aisle", "side", "pos"]).any()


def test_slotting_policies_are_valid_assignments():
    n_slots = sr.N_AISLES * sr.SIDES_PER_AISLE * sr.SLOTS_PER_SIDE
    pick_lines = np.array([50, 40, 40, 30, 20, 10, 5, 1])
    classes = np.array(["A", "A", "A", "B", "B", "C", "C", "C"])
    rng = np.random.default_rng(0)

    velocity = sr.slot_full_velocity(pick_lines)
    assert velocity.tolist() == list(range(len(pick_lines)))          # fastest SKU -> nearest slot

    cls = sr.slot_class_based(classes, rng)
    assert len(set(cls)) == len(cls)                                  # no two SKUs share a slot
    assert set(cls[:3]) == {0, 1, 2} and set(cls[3:5]) == {3, 4} and set(cls[5:]) == {5, 6, 7}

    rnd = sr.slot_random(len(pick_lines), n_slots, rng)
    assert len(set(rnd)) == len(rnd) and rnd.max() < n_slots


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"{len(tests)} tests passed")
