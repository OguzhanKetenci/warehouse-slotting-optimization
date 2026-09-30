"""
Module 7 - Optimal Routing: how far are the simple routing rules from the optimal route, and
does the best slotting scenario change when routing is optimal?

Same layout, orders and slotting scenarios as Modules 2 and 5 (imported, not re-implemented):
  - Slotting: Random (baseline), Class-based ABC, Full velocity (Module 2), Aisle-based
    velocity (Module 5). Random and Class-based ABC use Module 2's seeds (N_SEEDS below).
  - Routing: S-shape, Return, Largest gap (Module 2), Combined (this module), Optimal (this
    module: a TSP per order on the warehouse graph, solved with Google OR-Tools).

Warehouse graph (identical dimensions to Module 2)
  Nodes: the depot, every pick location (aisle, y) and the front and back end of every aisle.
  Edges: aisle segments between consecutive nodes of the same aisle, front and back cross-aisle
  segments between neighbouring aisle ends, and depot -> front end of aisle 0 (1.5 m).
  Shortest path between two locations (closed form, checked against Dijkstra):
    same aisle       : |y1 - y2|
    different aisles : |x1 - x2| + min(y1 + y2, 2L - y1 - y2)   (via the front or back cross aisle)
    depot -> (x, y)  : x + y
  All coordinates are multiples of 0.5 m, so distances x 2 are exact integers for OR-Tools.

Combined routing (this module; see docs/methodology.md)
  Aisles with picks are visited left to right. For each one the picker either traverses it end
  to end (switching between front and back cross aisle) or enters it from the side where they
  currently are and returns the same way. The choice per aisle is made by dynamic programming
  over the picker's side (front/back), so the route must end at the front; this is the
  "combined" heuristic of Roodbergen & de Koster (2001).

Optimal routing (OR-Tools routing solver, one TSP per order)
  Start from the better of the Combined and Largest-gap visiting sequences (so the result is never
  longer than any simple rule). Orders with <= GLS_MAX_NODES distinct locations: guided local
  search, stopped after GLS_SOLUTIONS solutions (deterministic). Larger orders: OR-Tools' default
  local search (2-opt, or-opt, relocate, exchange, Lin-Kernighan, ...) until no move improves the
  tour. Plain local search alone gets stuck even on 5-location orders (see the tests), which is
  why GLS is used wherever the time budget allows. Distance to the true optimum is measured in
  the validation step (exact Held-Karp on orders with <= 10 SKUs; a 3 s GLS on larger ones).

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv, data/processed/routing_sensitivity.csv
Run   : python src/07_optimal_routing.py              (full run, all orders)
        python src/07_optimal_routing.py --benchmark 500   (timing estimate only, writes nothing)
Output:
  data/processed/optimal_routing.csv             (one row per slotting scenario x routing policy)
  data/processed/optimal_routing_orders.csv.gz   (per order x layout run: all 5 route lengths)
  data/processed/optimal_routing_validation.csv  (OR-Tools vs. exact Held-Karp on orders with <= 10
                                                 SKUs, and vs. a 3 s guided local search on larger ones)
  reports/figures/08_optimal_routing_gap.png
"""
import argparse
import importlib.util
import os
import time
from multiprocessing import Pool
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sr = load_module("02_slotting_routing.py", "slotting_routing")
L = sr.AISLE_LENGTH_M
X0, PITCH = sr.FIRST_AISLE_X_M, sr.AISLE_PITCH_M

ROUTING_ORDER = ("S-shape", "Return", "Largest gap", "Combined", "Optimal")
RULES = ROUTING_ORDER[:-1]
# Seeds for Random / Class-based ABC: Module 2 uses 10 (0-9). The 500-order timing estimate put a
# 10-seed run at ~53 min (over the 30-min budget even with a time cap on large orders), so the
# first N_SEEDS of Module 2's seeds are used; the deterministic scenarios are unaffected.
N_SEEDS = 3
SMALL_ORDER_MAX_SKUS = 10         # validation against the exact Held-Karp solution
N_VALIDATION_ORDERS = 200
N_GLS_CHECK_ORDERS = 100          # larger orders re-solved with a long guided local search
GLS_CHECK_MS = 3000
VALIDATION_SEED = 7
# OR-Tools search (solve_order): guided local search stopped after GLS_SOLUTIONS solutions for
# orders with <= GLS_MAX_NODES distinct locations; default local search (to a local optimum) above.
GLS_SOLUTIONS = 50
GLS_MAX_NODES = 40
MAX_GLS_TIME_S = 1.0              # safety cap per order; only reached by unusually slow orders
SCALE = 2                         # distances x 2 -> exact integers (coordinates are multiples of 0.5 m)
ROUTING_COLORS = {"S-shape": "#2a78d6", "Return": "#eb6834", "Largest gap": "#1baf7a",
                  "Combined": "#eda100", "Optimal": "#e87ba4"}   # categorical slots 1-5

INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# ----------------------------------------------------------------------------
# Distance matrix: closed form and Dijkstra on the explicit graph
# ----------------------------------------------------------------------------
def aisle_x(aisle):
    return X0 + PITCH * np.asarray(aisle, dtype=float)


def distance_matrix(aisles, ys) -> np.ndarray:
    """Shortest-path distances between the depot (index 0) and the pick locations (1..n)."""
    a = np.r_[-1, np.asarray(aisles)]                     # depot is not in any aisle
    x = np.r_[0.0, aisle_x(aisles)]
    y = np.r_[0.0, np.asarray(ys, dtype=float)]
    same = a[:, None] == a[None, :]
    via_cross = np.abs(x[:, None] - x[None, :]) + np.minimum(y[:, None] + y[None, :],
                                                             2 * L - y[:, None] - y[None, :])
    return np.where(same, np.abs(y[:, None] - y[None, :]), via_cross)


def graph_distance_matrix(aisles, ys, n_aisles: int = sr.N_AISLES) -> np.ndarray:
    """Same matrix as distance_matrix, computed with Dijkstra on the explicit warehouse graph.

    Node ids: 0 = depot, 1..n = pick locations, then front end and back end of every aisle.
    """
    aisles, ys = np.asarray(aisles), np.asarray(ys, dtype=float)
    n = len(aisles)
    front = lambda j: 1 + n + 2 * j
    back = lambda j: 2 + n + 2 * j
    edges = [(0, front(0), X0)]
    for j in range(n_aisles - 1):
        edges += [(front(j), front(j + 1), PITCH), (back(j), back(j + 1), PITCH)]
    for j in range(n_aisles):
        idx = np.flatnonzero(aisles == j)
        idx = idx[np.argsort(ys[idx], kind="stable")]
        chain = [(front(j), 0.0)] + [(1 + i, ys[i]) for i in idx] + [(back(j), L)]
        edges += [(u, v, yv - yu) for (u, yu), (v, yv) in zip(chain, chain[1:])]
    u, v, w = map(np.array, zip(*edges))
    w = np.where(w == 0, 1e-12, w)          # csgraph drops explicit zeros; co-located nodes stay linked
    size = 1 + n + 2 * n_aisles
    g = coo_matrix((np.r_[w, w], (np.r_[u, v], np.r_[v, u])), shape=(size, size)).tocsr()
    d = dijkstra(g, directed=False, indices=np.arange(n + 1))[:, : n + 1]
    return np.round(d, 9)


# ----------------------------------------------------------------------------
# Exact TSP (validation only): Held-Karp dynamic programming
# ----------------------------------------------------------------------------
def held_karp(d: np.ndarray) -> float:
    """Exact shortest closed tour from node 0 through all other nodes (n <= ~13)."""
    n = len(d) - 1
    if n == 0:
        return 0.0
    full = 1 << n
    dp = np.full((full, n), np.inf)
    for j in range(n):
        dp[1 << j, j] = d[0, j + 1]
    dd = d[1:, 1:]
    for mask in range(1, full):
        row = dp[mask]
        if not np.isfinite(row).any():
            continue
        # extend to every city k not in mask: dp[mask | k, k] = min_j dp[mask, j] + d[j, k]
        best = (row[:, None] + dd).min(axis=0)
        for k in range(n):
            if not mask >> k & 1:
                nm = mask | (1 << k)
                if best[k] < dp[nm, k]:
                    dp[nm, k] = best[k]
    return float((dp[full - 1] + d[1:, 0]).min())


# ----------------------------------------------------------------------------
# OR-Tools TSP
# ----------------------------------------------------------------------------
def tour_length(d: np.ndarray, tour) -> float:
    return float(sum(d[a, b] for a, b in zip(tour, tour[1:])))


def solve_tsp(d: np.ndarray, initial: list | None = None, gls_solutions: int | None = None,
              gls_ms: int | None = None) -> tuple[float, list]:
    """Shortest closed tour from node 0 with OR-Tools' routing solver.

    initial       : optional visiting order of nodes 1..n-1 used as the starting solution
                    (otherwise OR-Tools builds one with PATH_CHEAPEST_ARC).
    gls_solutions : guided local search, stopped after this many solutions (deterministic), with
                    MAX_GLS_TIME_S as a safety cap on wall-clock time.
    gls_ms        : guided local search, stopped after this many milliseconds (validation only).
    Neither given : OR-Tools' default local search until no move improves the tour.
    Returns (tour length in metres, visiting sequence of node indices starting and ending at 0).
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    n = len(d)
    if n <= 3:                                   # 0, 1 or 2 locations: every tour is the same length
        tour = list(range(n)) + [0]
        return tour_length(d, tour), tour
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitMatrix(np.rint(d * SCALE).astype(np.int64).tolist())
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    if gls_solutions is not None or gls_ms is not None:
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        if gls_solutions is not None:
            params.solution_limit = int(gls_solutions)
            params.time_limit.FromMilliseconds(int(MAX_GLS_TIME_S * 1000))
        else:
            params.time_limit.FromMilliseconds(int(gls_ms))
    if initial is not None:
        routing.CloseModelWithParameters(params)
        start = routing.ReadAssignmentFromRoutes([list(initial)], True)
        if start is None:
            raise RuntimeError("OR-Tools rejected the initial route")
        solution = routing.SolveFromAssignmentWithParameters(start, params)
    else:
        solution = routing.SolveWithParameters(params)
    if solution is None:
        raise RuntimeError("OR-Tools found no solution")
    tour, idx = [], routing.Start(0)
    while not routing.IsEnd(idx):
        tour.append(manager.IndexToNode(idx))
        idx = solution.Value(routing.NextVar(idx))
    tour.append(0)
    return tour_length(d, tour), tour


# ----------------------------------------------------------------------------
# Combined routing (per-aisle traverse-or-return choice, dynamic programming over the side)
# ----------------------------------------------------------------------------
def combined_distance(aisles, ys) -> float:
    """Combined-heuristic route length for ONE order (reference implementation).

    Aisles with picks are visited left to right. In each, the picker either traverses it end to
    end (cost L, switches cross aisle) or enters from the cross aisle they are on and walks back
    out the same way (from the front: 2 * farthest pick; from the back: 2 * (L - nearest pick)).
    f / b = shortest vertical distance so far ending at the front / back cross aisle; the route
    must end at the front. Horizontal part: 2 * x(rightmost aisle), as in the other rules.
    """
    per = {}
    for a, y in zip(aisles, ys):
        lo, hi = per.get(a, (y, y))
        per[a] = (min(lo, y), max(hi, y))
    f, b = 0.0, np.inf
    for a in sorted(per):
        lo, hi = per[a]
        f, b = min(f + 2 * hi, b + L), min(b + 2 * (L - lo), f + L)
    return 2 * float(aisle_x(max(per))) + f


def combined_sequence(aisles, ys) -> list:
    """Indices of the picks in the order the Combined route (combined_distance) first visits them."""
    aisles, ys = np.asarray(aisles), np.asarray(ys, dtype=float)
    groups = [np.flatnonzero(aisles == a) for a in np.unique(aisles)]
    groups = [g[np.argsort(ys[g], kind="stable")] for g in groups]          # each: front -> back
    # forward DP keeping the choice that produced each state, then backtrack
    f, b, choice = 0.0, np.inf, []
    for g in groups:
        lo, hi = ys[g[0]], ys[g[-1]]
        f_opts = (f + 2 * hi, b + L)            # stay front (return from front) | traverse back -> front
        b_opts = (b + 2 * (L - lo), f + L)      # stay back (return from back)   | traverse front -> back
        choice.append((int(np.argmin(f_opts)), int(np.argmin(b_opts))))
        f, b = min(f_opts), min(b_opts)
    seq, side = [], "f"                          # the route ends at the front
    for g, (cf, cb) in zip(reversed(groups), reversed(choice)):
        if side == "f":
            enter = "f" if cf == 0 else "b"      # return from front entered at front; traverse entered at back
        else:
            enter = "b" if cb == 0 else "f"
        seq.append(list(g) if enter == "f" else list(g[::-1]))
        side = enter
    return [i for part in reversed(seq) for i in part]


def largest_gap_sequence(aisles, ys) -> list:
    """Indices of the picks in the order the Largest-gap route (Module 2) first visits them."""
    aisles, ys = np.asarray(aisles), np.asarray(ys, dtype=float)
    groups = [np.flatnonzero(aisles == a) for a in np.unique(aisles)]
    groups = [g[np.argsort(ys[g], kind="stable")] for g in groups]
    if len(groups) == 1:
        return list(groups[0])
    back_parts, front_parts = [], []
    for g in groups[1:-1]:
        bounds = np.r_[0.0, ys[g], L]
        gap = int(np.argmax(np.diff(bounds)))    # picks g[:gap] are before the gap, g[gap:] after it
        front_parts.append(list(g[:gap]))
        back_parts.append(list(g[gap:][::-1]))
    seq = list(groups[0])                                     # first aisle, front -> back
    for part in back_parts:                                   # back cross aisle, left -> right
        seq += part
    seq += list(groups[-1][::-1])                             # last aisle, back -> front
    for part in reversed(front_parts):                        # front cross aisle, right -> left
        seq += part
    return seq


def combined_distances(order_idx, aisle, y) -> np.ndarray:
    """Vectorised Combined length for every order (same recursion as combined_distance)."""
    per = (pd.DataFrame({"order": order_idx, "aisle": aisle, "y": y})
             .groupby(["order", "aisle"], sort=True)["y"].agg(["min", "max"]).reset_index())
    per["k"] = per.groupby("order").cumcount()
    n_orders = int(per["order"].max()) + 1
    f, b = np.zeros(n_orders), np.full(n_orders, np.inf)
    for k in range(int(per["k"].max()) + 1):
        step = per[per["k"] == k]
        o = step["order"].to_numpy()
        lo, hi = step["min"].to_numpy(), step["max"].to_numpy()
        f_new = np.minimum(f[o] + 2 * hi, b[o] + L)
        b_new = np.minimum(b[o] + 2 * (L - lo), f[o] + L)
        f[o], b[o] = f_new, b_new
    horizontal = 2 * aisle_x(per.groupby("order")["aisle"].max().to_numpy())
    return horizontal + f


# ----------------------------------------------------------------------------
# Per-order optimal routes (parallel)
# ----------------------------------------------------------------------------
def solve_order(aisle, y, gls_solutions=GLS_SOLUTIONS, gls_max_nodes=GLS_MAX_NODES,
                gls_ms=None) -> tuple[float, float, int]:
    """Optimal route length of ONE order: OR-Tools, warm-started from the better of the Combined
    and Largest-gap visiting sequences (so the result can never be longer than either rule).

    Picks at the same location (two SKUs facing each other across the aisle) are one node.
    Orders with <= gls_max_nodes locations: guided local search stopped after gls_solutions
    solutions. Larger orders: default local search to a local optimum. gls_ms (validation only)
    overrides both and runs guided local search for that many milliseconds on any order.
    Returns (optimal length, warm-start length, number of distinct locations).
    """
    loc, inv = np.unique(np.column_stack([aisle, y]), axis=0, return_inverse=True)
    inv = inv.ravel()
    d = distance_matrix(loc[:, 0].astype(int), loc[:, 1])
    start = None
    for seq_fn in (combined_sequence, largest_gap_sequence):
        nodes = list(dict.fromkeys((inv[seq_fn(aisle, y)] + 1).tolist()))
        length = tour_length(d, [0] + nodes + [0])
        if start is None or length < start[0]:
            start = (length, nodes)
    if gls_ms is not None:
        opt = solve_tsp(d, initial=start[1], gls_ms=gls_ms)[0]
    else:
        use_gls = gls_solutions is not None and len(loc) <= gls_max_nodes
        opt = solve_tsp(d, initial=start[1], gls_solutions=gls_solutions if use_gls else None)[0]
    return opt, start[0], len(loc)


def _solve_chunk(args):
    """Worker: (list of (aisle array, y array), gls_solutions, gls_max_nodes) -> solve_order results."""
    chunk, gls_solutions, gls_max_nodes = args
    return [solve_order(a, y, gls_solutions, gls_max_nodes) for a, y in chunk]


def split_orders(order_idx, aisle, y):
    order_sorted = np.argsort(order_idx, kind="stable")
    bounds = np.flatnonzero(np.diff(order_idx[order_sorted])) + 1
    return [(aisle[s], y[s]) for s in np.split(order_sorted, bounds)]


def optimal_distances(order_idx, aisle, y, pool, gls_solutions=None, gls_max_nodes=None, chunk_work=20_000):
    """(optimal, warm-start, n_locations) arrays for every order, solved in parallel."""
    orders = split_orders(order_idx, aisle, y)
    # Largest orders first, and chunks balanced by estimated work (~ n^2), so one worker does not
    # end up with all the large orders while the others sit idle.
    sizes = np.array([len(a) for a, _ in orders])
    order = np.argsort(-sizes, kind="stable")
    chunks, current, work = [], [], 0
    for i in order:
        current.append(orders[i])
        work += max(sizes[i], 10) ** 2
        if work >= chunk_work:
            chunks.append((current, gls_solutions, gls_max_nodes))
            current, work = [], 0
    if current:
        chunks.append((current, gls_solutions, gls_max_nodes))
    results = np.array([r for part in pool.map(_solve_chunk, chunks, chunksize=1) for r in part])
    out = np.empty_like(results)
    out[order] = results
    return out[:, 0], out[:, 1], out[:, 2].astype(int)


# ----------------------------------------------------------------------------
# Layouts (Module 2 and Module 5 slotting functions)
# ----------------------------------------------------------------------------
def layouts(sku, layout, m5, n_seeds):
    sku_class = sku["abc_pick"].to_numpy()
    pick_lines = sku["pick_lines"].to_numpy()
    n_skus, n_slots = len(sku), len(layout)
    for seed in sr.RANDOM_SEEDS[:n_seeds]:
        yield sr.SCENARIO_RANDOM, seed, sr.slot_random(n_skus, n_slots, np.random.default_rng(seed))
    for seed in sr.RANDOM_SEEDS[:n_seeds]:
        yield sr.SCENARIO_CLASS, seed, sr.slot_class_based(sku_class, np.random.default_rng(seed))
    yield sr.SCENARIO_VELOCITY, None, sr.slot_full_velocity(pick_lines)
    yield m5.SCENARIO_AISLE, None, m5.slot_aisle_based_velocity(pick_lines, layout)


def heuristic_distances(visit_order, aisle, y) -> dict:
    return {"S-shape": sr.s_shape_distances(visit_order, aisle, y),
            "Return": sr.return_distances(visit_order, aisle, y),
            "Largest gap": sr.largest_gap_distances(visit_order, aisle, y),
            "Combined": combined_distances(visit_order, aisle, y)}


def pick_coordinates(ranks, layout, visit_sku):
    slot = ranks[visit_sku]
    return layout["aisle"].to_numpy()[slot], layout["y_m"].to_numpy()[slot]


# ----------------------------------------------------------------------------
# Benchmark (timing estimate on a subset of orders; writes nothing)
# ----------------------------------------------------------------------------
def benchmark(n_orders, layout, sku, visit_order, visit_sku, m5, n_workers, gls_solutions, gls_max_nodes):
    rng = np.random.default_rng(0)
    chosen = np.sort(rng.choice(int(visit_order.max()) + 1, size=n_orders, replace=False))
    keep = np.isin(visit_order, chosen)
    vo = pd.factorize(visit_order[keep])[0]
    vs = visit_sku[keep]
    total_orders = int(visit_order.max()) + 1
    print(f"Benchmark: {n_orders} random orders of {total_orders:,}, {n_workers} worker processes, "
          f"GLS ({gls_solutions} solutions) for orders with <= {gls_max_nodes} locations")
    with Pool(n_workers) as pool:
        per_layout = []
        for scenario, seed, ranks in layouts(sku, layout, m5, 1):
            a, y = pick_coordinates(ranks, layout, vs)
            t = time.perf_counter()
            opt, warm, _ = optimal_distances(vo, a, y, pool, gls_solutions, gls_max_nodes)
            dt = time.perf_counter() - t
            heur = heuristic_distances(vo, a, y)
            worst = max(float((opt - h).max()) for h in heur.values())
            warm_excess = float((warm - np.minimum(heur["Combined"], heur["Largest gap"])).max())
            per_layout.append(dt)
            print(f"  {scenario:<22} seed={seed}: {dt:6.1f} s, mean optimal {opt.mean():7.2f} m, "
                  f"mean warm start {warm.mean():7.2f} m, max(optimal - rule) = {worst:+.2f} m, "
                  f"max(warm - min(Combined, LG)) = {warm_excess:+.2f} m")
    per_order = np.mean(per_layout) / n_orders
    for seeds in (10, 3):
        n_layouts = 2 * seeds + 2
        est = per_order * total_orders * n_layouts
        print(f"Estimated full run with {seeds} seeds ({n_layouts} layouts x {total_orders:,} orders): "
              f"{est / 60:.1f} min")


# ----------------------------------------------------------------------------
# Full evaluation
# ----------------------------------------------------------------------------
def run_all(layout, sku, visit_order, visit_sku, m5, pool):
    parts = []
    for scenario, seed, ranks in layouts(sku, layout, m5, N_SEEDS):
        t = time.perf_counter()
        a, y = pick_coordinates(ranks, layout, visit_sku)
        heur = heuristic_distances(visit_order, a, y)
        opt, warm, n_loc = optimal_distances(visit_order, a, y, pool, GLS_SOLUTIONS, GLS_MAX_NODES)
        part = pd.DataFrame({"scenario": scenario, "seed": seed, "order": np.arange(len(opt)),
                             "n_locations": n_loc, **heur, "Optimal": opt, "warm_start": warm})
        parts.append(part)
        print(f"  {scenario:<22} seed={str(seed):<4} {time.perf_counter() - t:6.1f} s | "
              + " | ".join(f"{r} {part[r].mean():6.1f}" for r in ROUTING_ORDER), flush=True)
    per_order = pd.concat(parts, ignore_index=True)
    per_order["seed"] = per_order["seed"].astype("Int64")
    return per_order


def check_per_order(per_order: pd.DataFrame) -> None:
    """Hard checks on every order: optimal <= every rule, Combined <= min(S-shape, Return)."""
    for rule in RULES:
        n_bad = int((per_order["Optimal"] > per_order[rule] + 1e-9).sum())
        if n_bad:
            raise AssertionError(f"Optimal longer than {rule} in {n_bad} orders")
    best_sr = np.minimum(per_order["S-shape"], per_order["Return"])
    n_bad = int((per_order["Combined"] > best_sr + 1e-9).sum())
    if n_bad:
        raise AssertionError(f"Combined longer than min(S-shape, Return) in {n_bad} orders")
    print(f"Check OK: Optimal <= S-shape, Return, Largest gap and Combined in all {len(per_order):,} "
          f"order x layout rows; Combined <= min(S-shape, Return) in all of them.")


def check_against_module5(per_order: pd.DataFrame, m5) -> None:
    """Deterministic scenarios must reproduce Module 5's S-shape / Return / Largest-gap means exactly."""
    m5_res = pd.read_csv(PROC / "routing_sensitivity.csv").set_index(["routing", "scenario"])
    for scenario in (sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE):
        sub = per_order[per_order["scenario"] == scenario]
        for rule in ("S-shape", "Return", "Largest gap"):
            mine, theirs = sub[rule].mean(), m5_res.loc[(rule, scenario), "avg_distance_per_order_m"]
            if abs(mine - theirs) > 1e-6:
                raise AssertionError(f"{rule}/{scenario}: {mine} != Module 5's {theirs}")
    print("Check OK: Full velocity and Aisle-based velocity under S-shape / Return / Largest gap match "
          "Module 5's routing_sensitivity.csv exactly.")


def _validate_small(order):
    aisle, y = order
    opt, _, n = solve_order(aisle, y)
    loc = np.unique(np.column_stack([aisle, y]), axis=0)
    return n, opt, held_karp(distance_matrix(loc[:, 0].astype(int), loc[:, 1]))


def _validate_large(order):
    aisle, y = order
    opt, _, n = solve_order(aisle, y)
    ref, _, _ = solve_order(aisle, y, gls_ms=GLS_CHECK_MS)
    return n, opt, ref


def validate(layout, sku, visit_order, visit_sku, m5, pool) -> pd.DataFrame:
    """(1) OR-Tools vs. exact Held-Karp on N_VALIDATION_ORDERS random orders with <= 10 SKUs,
    evaluated in every scenario (seed 0 for Random / Class-based ABC).
    (2) OR-Tools (as used) vs. a GLS_CHECK_MS guided local search from the same warm start on
    N_GLS_CHECK_ORDERS random orders with more than 10 SKUs (Random, seed 0): a diagnostic of the
    remaining gap on orders too large for an exact solution (GLS is not exact either)."""
    rng = np.random.default_rng(VALIDATION_SEED)
    skus_per_order = np.bincount(visit_order)
    small = np.sort(rng.choice(np.flatnonzero(skus_per_order <= SMALL_ORDER_MAX_SKUS),
                               N_VALIDATION_ORDERS, replace=False))
    large = np.sort(rng.choice(np.flatnonzero(skus_per_order > SMALL_ORDER_MAX_SKUS),
                               N_GLS_CHECK_ORDERS, replace=False))
    rows = []
    for scenario, seed, ranks in layouts(sku, layout, m5, 1):
        a, y = pick_coordinates(ranks, layout, visit_sku)
        orders = split_orders(visit_order, a, y)
        checks = [("Held-Karp (exact)", small, _validate_small, 5)]
        if scenario == sr.SCENARIO_RANDOM:
            checks.append((f"GLS {GLS_CHECK_MS / 1000:.0f} s", large, _validate_large, 1))
        for name, chosen, fn, chunk in checks:
            res = pool.map(fn, [orders[i] for i in chosen], chunksize=chunk)
            rows += [{"check": name, "scenario": scenario, "seed": seed, "order": o, "n_skus": skus_per_order[o],
                      "n_locations": n, "ortools_m": ot, "reference_m": ref}
                     for o, (n, ot, ref) in zip(chosen, res)]
    val = pd.DataFrame(rows)
    val["seed"] = val["seed"].astype("Int64")
    val["gap_pct"] = (val["ortools_m"] / val["reference_m"] - 1) * 100
    return val


def summarize(per_order: pd.DataFrame, scenario_order: list) -> pd.DataFrame:
    """One row per scenario x routing. Excess vs. optimal is computed per order (rule / optimal - 1)
    and pooled over all orders and seeds (mean, median); excess_of_avg_distance_pct is the same
    comparison on the average distances instead."""
    rows = []
    for scenario in scenario_order:
        sub = per_order[per_order["scenario"] == scenario]
        run_means = sub.groupby("seed", dropna=False)[list(ROUTING_ORDER)].mean()
        for routing in ROUTING_ORDER:
            excess = (sub[routing] / sub["Optimal"] - 1) * 100
            rows.append({"scenario": scenario, "routing": routing, "n_runs": len(run_means),
                         "avg_distance_per_order_m": run_means[routing].mean(),
                         "avg_distance_per_order_min_m": run_means[routing].min(),
                         "avg_distance_per_order_max_m": run_means[routing].max(),
                         "excess_vs_optimal_mean_pct": excess.mean(),
                         "excess_vs_optimal_median_pct": excess.median(),
                         "excess_of_avg_distance_pct":
                             (run_means[routing].mean() / run_means["Optimal"].mean() - 1) * 100})
    out = pd.DataFrame(rows)
    base = out[out["scenario"] == sr.SCENARIO_RANDOM].set_index("routing")["avg_distance_per_order_m"]
    out["change_vs_random"] = out["avg_distance_per_order_m"] / out["routing"].map(base) - 1
    non_random = out[out["scenario"] != sr.SCENARIO_RANDOM]
    best = non_random.groupby("routing")["avg_distance_per_order_m"].idxmin()
    out["best_scenario_for_routing"] = out.index.isin(best.to_numpy())
    return out


# ----------------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------------
def plot_gap(results: pd.DataFrame, scenario_order: list, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.6))
    n_r = len(ROUTING_ORDER)
    width = 0.84 / n_r
    x_base = np.arange(len(scenario_order))
    y_max = results["avg_distance_per_order_max_m"].max()
    for i, routing in enumerate(ROUTING_ORDER):
        xs = x_base + (i - (n_r - 1) / 2) * width
        sub = results[results["routing"] == routing].set_index("scenario").reindex(scenario_order)
        means = sub["avg_distance_per_order_m"].to_numpy()
        ax.bar(xs, means, width - 0.02, color=ROUTING_COLORS[routing], label=routing, zorder=2)
        multi = sub["n_runs"].to_numpy() > 1
        lo = means - sub["avg_distance_per_order_min_m"].to_numpy()
        hi = sub["avg_distance_per_order_max_m"].to_numpy() - means
        if multi.any():
            ax.errorbar(xs[multi], means[multi], yerr=[lo[multi], hi[multi]], fmt="none",
                        ecolor=INK, elinewidth=0.9, capsize=2, zorder=3)
        for xi, m, h in zip(xs, means, hi):
            ax.text(xi, m + h + y_max * 0.012, f"{m:,.0f}", ha="center", va="bottom", fontsize=7.8,
                    color=INK, rotation=90)
    ax.set_xticks(x_base)
    ax.set_xticklabels(scenario_order, color=INK, fontsize=10.5)
    ax.set_ylabel("Avg. distance per order (m)", color=INK_MUTED)
    ax.set_ylim(0, y_max * 1.22)
    ax.set_title("Picker travel distance by slotting scenario: simple routing rules vs. optimal route",
                 color=INK, loc="left", fontsize=11.5, pad=30)
    ax.legend(frameon=False, fontsize=9, loc="upper center", ncol=n_r, bbox_to_anchor=(0.5, 1.09))
    ax.text(0, -0.1, f"Bars = mean over {N_SEEDS} seeds (Random, Class-based ABC; whiskers = min-max) or the "
                     "single deterministic run (Full velocity, Aisle-based velocity).\nAll 19,773 orders of "
                     "the last 12 months, same layout as Modules 2 and 5. Optimal = OR-Tools TSP per order.",
            transform=ax.transAxes, fontsize=8, color=INK_MUTED, va="top")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", type=int, default=0, help="time N random orders and exit")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--gls-solutions", type=int, default=GLS_SOLUTIONS)
    ap.add_argument("--gls-max-nodes", type=int, default=GLS_MAX_NODES)
    args = ap.parse_args()

    m5 = load_module("05_routing_sensitivity.py", "routing_sensitivity")
    layout = sr.build_layout()
    sku, visit_order, visit_sku = sr.load_inputs()
    if args.benchmark:
        benchmark(args.benchmark, layout, sku, visit_order, visit_sku, m5, args.workers,
                  args.gls_solutions, args.gls_max_nodes)
        return

    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    n_orders = int(visit_order.max()) + 1
    print(f"Layout : {len(layout):,} slots | Orders: {n_orders:,} | SKUs: {len(sku):,} | "
          f"order-SKU visits: {len(visit_order):,} | seeds for Random / Class-based ABC: {N_SEEDS} | "
          f"workers: {args.workers}")
    t0 = time.perf_counter()
    with Pool(args.workers) as pool:
        print("\n--- ROUTING ALL ORDERS (avg. distance per order, m) ---")
        per_order = run_all(layout, sku, visit_order, visit_sku, m5, pool)
        print(f"Main evaluation: {(time.perf_counter() - t0) / 60:.1f} min")
        check_per_order(per_order)
        check_against_module5(per_order, m5)
        print("\n--- VALIDATION ---")
        t1 = time.perf_counter()
        val = validate(layout, sku, visit_order, visit_sku, m5, pool)
        print(f"Validation: {(time.perf_counter() - t1) / 60:.1f} min")

    for check, sub in val.groupby("check", sort=False):
        exact = sub["gap_pct"].abs() < 1e-9
        print(f"OR-Tools vs. {check}: {len(sub)} order x scenario cases "
              f"({sub['n_locations'].min()}-{sub['n_locations'].max()} locations) | "
              f"gap mean {sub['gap_pct'].mean():.4f}% | median {sub['gap_pct'].median():.4f}% | "
              f"max {sub['gap_pct'].max():.4f}% | min {sub['gap_pct'].min():.4f}% | "
              f"identical in {int(exact.sum())} / {len(sub)}")

    scenario_order = [sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE]
    results = summarize(per_order, scenario_order)
    print("\n--- OPTIMAL ROUTING RESULTS ---")
    print(results.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    table = results.pivot(index="routing", columns="scenario", values="avg_distance_per_order_m")
    print("\n--- AVG. DISTANCE PER ORDER (m): routing x scenario ---")
    print(table.reindex(index=list(ROUTING_ORDER), columns=scenario_order).round(1).to_string())
    print("\n--- BEST (SHORTEST-DISTANCE) NON-RANDOM SLOTTING SCENARIO PER ROUTING POLICY ---")
    best = results[results["best_scenario_for_routing"]].set_index("routing").reindex(list(ROUTING_ORDER))
    print(best[["scenario", "avg_distance_per_order_m", "change_vs_random"]]
          .to_string(float_format=lambda v: f"{v:,.4f}"))

    results.to_csv(PROC / "optimal_routing.csv", index=False)
    per_order.to_csv(PROC / "optimal_routing_orders.csv.gz", index=False)
    val.to_csv(PROC / "optimal_routing_validation.csv", index=False)
    plot_gap(results, scenario_order, FIG / "08_optimal_routing_gap.png")
    print(f"\nTotal runtime: {(time.perf_counter() - t0) / 60:.1f} min")
    print(f"Figure -> {FIG / '08_optimal_routing_gap.png'}\nTables -> {PROC}")


if __name__ == "__main__":
    main()
