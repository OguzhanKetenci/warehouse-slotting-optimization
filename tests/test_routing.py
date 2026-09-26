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
