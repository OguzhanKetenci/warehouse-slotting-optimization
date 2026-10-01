"""
Module 8b - Refined Congestion Model: segment-level aisles, traffic-spreading slotting,
skip-and-return policy and cost-based team size

Extends Module 8a (src/08_congestion_simulation.py, kept unchanged as the "strict model" in
which a whole aisle holds one picker). Same orders, shift, replication, pick and handling times.

1. Segment-level aisles: every 50 m aisle is split into 10 segments of 5 m. A picker holds the
   segment they are in (walking or picking). Narrow aisle: 1 picker per segment; wide: 2.
   Deadlock rule ("step aside"): a picker releases their current segment BEFORE requesting the
   next one, so nobody ever holds one segment while waiting for another - circular waits, and
   so deadlocks, cannot occur. Physically: a blocked picker steps aside (or back to the segment
   boundary) and lets the occupant pass. See docs/methodology.md.
2. Spread velocity (K = 5, 10, 20): the fastest SKUs are dealt round-robin over the front
   positions of the first K aisles (depth first, then rack side, then aisle), instead of filling
   aisle 1 completely; the rest continue aisle by aisle as in Aisle-based velocity. K = 1 is
   exactly Module 5's Aisle-based velocity.
3. Skip-and-return: just before walking to the next aisle on the route, the picker checks that
   aisle's entry segment; if it is full (or has a queue) the picker swaps it with the following
   aisle on the route and returns to it straight after. Each aisle can be skipped once. Swapping
   two consecutive aisle visits never changes the side (front/back) the picker ends on, so the
   rest of the route stays valid; a visit entered from the other side than planned is walked in
   mirror order.
4. Cost-based team size: the smallest team meeting a service level - >= 95% of orders completed
   on the day they arrive (before midnight) and, on >= 95% of days, overtime per picker
   (day's overtime / team size) <= 30 min - next to Module 8a's elasticity definition. Unit
   cost = pickers x shift hours x wage + overtime hours x 1.5 x wage (wage = 1 unit/hour).

Input : as Module 8a, plus data/processed/optimal_tours.pkl (Module 8a cache) and
        data/processed/congestion_simulation.csv (Module 8a results, for the strict-model column)
Run   : python src/08b_congestion_refined.py --estimate   (runtime estimate only)
        python src/08b_congestion_refined.py              (full run)
        python src/08b_congestion_refined.py --plot-only  (redraw figures from the CSV)
        python src/08b_congestion_refined.py --rerun-team-size  (re-run the team-size set only; used
                                                  after the overtime definition was corrected)
Output:
  data/processed/optimal_tours_spread.pkl       (cache: optimal tours for the Spread layouts)
  data/processed/congestion_refined.csv
  reports/figures/10_congestion_refined.png
  reports/figures/linkedin_congestion_refined.png
"""
import argparse
import importlib.util
import os
import pickle
import time
from multiprocessing import Pool
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import simpy

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m8 = load_module("08_congestion_simulation.py", "congestion")
sr, m5, m7 = m8.sr, m8.m5, m8.m7
L = sr.AISLE_LENGTH_M

# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------
SEGMENT_M = 5.0
N_SEGMENTS = int(L / SEGMENT_M)            # 10 per aisle
AISLE_MODELS = {"narrow": 1, "wide": 2}    # pickers per segment
POLICIES = ("wait", "skip")
SPREAD_K = (5, 10, 20)
PICK_TIME_S = m8.PICK_TIME_S               # 10 s per SKU
ORDER_HANDLING_S = m8.ORDER_HANDLING_S     # 60 s per order
SHIFT_START_H, SHIFT_END_H = m8.SHIFT_START_H, m8.SHIFT_END_H
VOLUME_MULTIPLIERS = m8.VOLUME_MULTIPLIERS
WAGE_PER_H = 1.0                           # cost unit per picker-hour
OVERTIME_FACTOR = 1.5
SLA_SAME_DAY_PCT = 95.0                    # orders completed before midnight of their arrival day
SLA_OVERTIME_DAYS_PCT = 95.0               # share of days with overtime per picker <= limit
SLA_OVERTIME_PER_PICKER_H = 0.5
KNEE_ELASTICITY = m8.KNEE_ELASTICITY       # Module 8a's speed-based definition
PICKER_GRID = {1: (3, 4, 5, 6, 8), 5: (10, 12, 14, 16, 20), 10: (18, 23, 28, 34, 40)}
REFERENCE = {"volume": 10, "pickers": 23, "route": "S-shape"}
SPREAD_TOUR_CACHE = PROC / "optimal_tours_spread.pkl"

SCENARIO_RANDOM, SCENARIO_CLASS = sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS
SCENARIO_VELOCITY, SCENARIO_AISLE = sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE
spread_name = lambda k: f"Spread velocity K={k}"
SCENARIOS = [SCENARIO_RANDOM, SCENARIO_CLASS, SCENARIO_VELOCITY, SCENARIO_AISLE] + [spread_name(k) for k in SPREAD_K]

INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
COLORS = {SCENARIO_RANDOM: "#2a78d6", SCENARIO_CLASS: "#eb6834", SCENARIO_VELOCITY: "#1baf7a",
          SCENARIO_AISLE: "#eda100", spread_name(5): "#e87ba4", spread_name(10): "#008300",
          spread_name(20): "#8a5cd1"}


# ----------------------------------------------------------------------------
# Spread velocity slotting
# ----------------------------------------------------------------------------
def slot_spread_velocity(pick_lines: np.ndarray, layout: pd.DataFrame, k: int) -> np.ndarray:
    """Fastest SKUs dealt round-robin over the first k aisles: slot order = depth (position),
    then rack side, then aisle, within aisles 0..k-1; after those k x 100 slots, aisle by aisle
    (aisle, position, side) as in Module 5's slot_aisle_based_velocity. k = 1 reproduces it."""
    in_front = (layout["aisle"] < k).to_numpy()
    key = layout.assign(group=np.where(in_front, 0, 1),
                        a1=np.where(in_front, layout["pos"], layout["aisle"]),
                        a2=np.where(in_front, layout["side"], layout["pos"]),
                        a3=np.where(in_front, layout["aisle"], layout["side"]))
    slot_order = key.sort_values(["group", "a1", "a2", "a3"], kind="mergesort").index.to_numpy()
    order = np.argsort(-pick_lines, kind="stable")
    ranks = np.empty(len(pick_lines), dtype=int)
    ranks[order] = slot_order[: len(pick_lines)]
    return ranks


def layout_ranks(sku, layout):
    """[(scenario, ranks)] for all 7 layouts (Random / ABC: Module 2's seed 0)."""
    runs = [(s, ranks) for s, seed, ranks in m7.layouts(sku, layout, m5, 1) if seed in (None, 0)]
    pick_lines = sku["pick_lines"].to_numpy()
    runs += [(spread_name(k), slot_spread_velocity(pick_lines, layout, k)) for k in SPREAD_K]
    return runs


# ----------------------------------------------------------------------------
# Routes as aisle visits with pick positions
# A visit: (aisle, x, entry_side, traverse, picks) - entry_side 0 = front, 1 = back; traverse
# = leaves at the opposite end (else the same end); picks = [(y, n_skus), ...] in visiting order.
# ----------------------------------------------------------------------------
def s_shape_visits(aisles, ys):
    """Module 2's S-shape route as visits (same geometry as m8.s_shape_route)."""
    aisles, ys = np.asarray(aisles), np.asarray(ys, dtype=float)
    uniq = np.unique(aisles)
    odd = len(uniq) % 2
    visits = []
    for i, a in enumerate(uniq):
        yy, cnt = np.unique(ys[aisles == a], return_counts=True)
        side = i % 2                                        # front for 1st, 3rd, ...; back for 2nd, ...
        picks = list(zip(yy.tolist(), cnt.tolist()))
        if side == 1:
            picks = picks[::-1]
        last_return = odd and i == len(uniq) - 1
        visits.append((int(a), float(m7.aisle_x(a)), side, not last_return, picks))
    return visits


def tour_visits(loc_aisle, loc_y, loc_count, tour):
    """Module 7's optimal tour as visits (same walking rule as m8.tour_route)."""
    visits, cur, side, picks = [], None, 0, []
    for node in tour[1:-1]:
        a, y, c = int(loc_aisle[node - 1]), float(loc_y[node - 1]), int(loc_count[node - 1])
        if cur is None:
            side, picks = 0, [(y, c)]
        elif cur[0] == a:
            picks.append((y, c))
        else:
            front = cur[1] + y <= 2 * L - cur[1] - y
            exit_side = 0 if front else 1
            visits.append((cur[0], float(m7.aisle_x(cur[0])), side, exit_side != side, picks))
            side, picks = exit_side, [(y, c)]
        cur = (a, y)
    visits.append((cur[0], float(m7.aisle_x(cur[0])), side, side != 0, picks))   # leave by the front
    return visits


def oriented(visit, side):
    """Waypoints (y) and pick dwell counts for walking `visit` when entering from `side`.
    The planned order is kept when side == planned entry; otherwise picks are walked in order of
    distance from the actual entry end. Returns (waypoints, counts, exit_side)."""
    _, _, entry, traverse, picks = visit
    if side != entry:
        picks = sorted(picks, key=lambda p: p[0], reverse=side == 1)
    start = 0.0 if side == 0 else L
    exit_side = 1 - side if traverse else side
    end = 0.0 if exit_side == 0 else L
    return [start] + [p[0] for p in picks] + [end], [0] + [p[1] for p in picks] + [0], exit_side


def segment_of(y: float) -> int:
    return min(int(y // SEGMENT_M), N_SEGMENTS - 1)


def timeline(waypoints, counts, pick_time_s=PICK_TIME_S):
    """Consecutive (segment, seconds) pieces for walking the waypoints at 1 m/s and picking
    counts[i] SKUs at waypoint i. Returns (pieces, walk_s)."""
    pieces, walk = [], 0.0

    def add(seg, t):
        if pieces and pieces[-1][0] == seg:
            pieces[-1][1] += t
        else:
            pieces.append([seg, t])

    for i, y in enumerate(waypoints):
        if i:
            y0 = waypoints[i - 1]
            lo, hi = min(y0, y), max(y0, y)
            bounds = [b * SEGMENT_M for b in range(int(lo // SEGMENT_M) + 1, int(np.ceil(hi / SEGMENT_M)))]
            pts = [y0] + (bounds if y >= y0 else bounds[::-1]) + [y]
            for p, q in zip(pts, pts[1:]):
                if q != p:
                    add(segment_of((p + q) / 2), abs(q - p) / m8.WALK_SPEED_M_S)
                    walk += abs(q - p) / m8.WALK_SPEED_M_S
        if counts[i]:
            add(segment_of(y), counts[i] * pick_time_s)
    if not pieces:                               # degenerate (cannot happen for real visits)
        pieces.append([segment_of(waypoints[0]), 0.0])
    return tuple((s, t) for s, t in pieces), walk


def visits_walk_m(visits) -> float:
    """Walking length of a planned route (cross aisles + inside aisles + back to the depot)."""
    x, side, total = 0.0, 0, 0.0
    for v in visits:
        total += abs(v[1] - x)
        wp, _, side = oriented(v, side)
        total += sum(abs(b - a) for a, b in zip(wp, wp[1:]))
        x = v[1]
    return total + x


def prepare_routes(visit_lists, pick_time_s=PICK_TIME_S):
    """Per order: list of [visit, planned timeline, planned walk_s, planned exit side]."""
    out = []
    for visits in visit_lists:
        side, prepared = 0, []
        for v in visits:
            wp, cnt, exit_side = oriented(v, side)
            tl, walk = timeline(wp, cnt, pick_time_s)
            prepared.append((v, tl, walk, side))
            side = exit_side
        out.append(tuple(prepared))
    return out


# ----------------------------------------------------------------------------
# Simulation (one day)
# ----------------------------------------------------------------------------
def simulate_day(arrivals, base_idx, routes, n_pickers, capacity, policy="wait",
                 n_aisles=sr.N_AISLES, shift_start_s=SHIFT_START_H * 3600,
                 handling_s=ORDER_HANDLING_S, pick_time_s=PICK_TIME_S):
    """routes: prepare_routes output. capacity: pickers per segment (None = unlimited).
    Returns dict of per-order arrays (start, end, wait, walk, skips), per-picker last finish,
    and per-aisle waiting."""
    env = simpy.Environment()
    queue = simpy.Store(env)
    segs = None if capacity is None else [[simpy.Resource(env, capacity=capacity) for _ in range(N_SEGMENTS)]
                                          for _ in range(n_aisles)]
    n = len(arrivals)
    out = {k: np.zeros(n) for k in ("start", "end", "wait", "walk", "skips")}
    last_finish = np.zeros(n_pickers)
    aisle_wait = np.zeros(n_aisles)

    def source():
        for i, t in enumerate(arrivals):
            if t > env.now:
                yield env.timeout(t - env.now)
            queue.put(i)

    def picker(p):
        yield env.timeout(shift_start_s)
        while True:
            i = yield queue.get()
            out["start"][i] = env.now
            plan = list(routes[base_idx[i]])
            postponed = [False] * len(plan)
            yield env.timeout(handling_s)              # set-up first, then the first routing decision
            x, side, pending = 0.0, 0, 0.0
            w = walk = 0.0
            skips = 0
            j = 0
            while j < len(plan):
                if (policy == "skip" and segs is not None and j + 1 < len(plan) and not postponed[j]):
                    entry = segs[plan[j][0][0]][0 if side == 0 else N_SEGMENTS - 1]
                    if entry.count >= entry.capacity or entry.queue:
                        plan[j], plan[j + 1] = plan[j + 1], plan[j]
                        postponed[j], postponed[j + 1] = postponed[j + 1], True
                        skips += 1
                v, tl, vwalk, planned_side = plan[j]
                if side != planned_side:                       # entered from the other end
                    wp, cnt, exit_side = oriented(v, side)
                    tl, vwalk = timeline(wp, cnt, pick_time_s)
                else:
                    exit_side = (1 - side) if v[3] else side
                cross = abs(v[1] - x)
                walk += cross + vwalk
                yield env.timeout(pending + cross)
                pending = 0.0
                if segs is None:
                    yield env.timeout(sum(t for _, t in tl))
                else:
                    col = segs[v[0]]
                    held = None
                    for seg, dur in tl:
                        if held is not None:
                            col[held[0]].release(held[1])      # step aside before requesting the next
                        req = col[seg].request()
                        if not req.triggered:
                            t0 = env.now
                            yield req
                            dw = env.now - t0
                            w += dw
                            aisle_wait[v[0]] += dw
                        held = (seg, req)
                        yield env.timeout(dur)
                    col[held[0]].release(held[1])
                x, side = v[1], exit_side
                j += 1
            if side != 0:
                raise AssertionError("route ended at the back cross aisle")
            walk += x
            yield env.timeout(pending + x)
            out["end"][i], out["wait"][i], out["walk"][i], out["skips"][i] = env.now, w, walk, skips
            last_finish[p] = env.now

    env.process(source())
    for p in range(n_pickers):
        env.process(picker(p))
    env.run()
    return out, last_finish, aisle_wait


# ----------------------------------------------------------------------------
# Configurations, workers and metrics
# ----------------------------------------------------------------------------
_CACHE = {}


def _inputs():
    """Per worker: orders, layouts, tours (loaded once)."""
    if "base" not in _CACHE:
        layout, sku, visit_order, visit_sku, orders = m8.load_orders()
        with open(m8.TOUR_CACHE, "rb") as fh:
            tours = pickle.load(fh)["tours"]
        if SPREAD_TOUR_CACHE.exists():
            with open(SPREAD_TOUR_CACHE, "rb") as fh:
                tours.update(pickle.load(fh)["tours"])
        _CACHE["base"] = (layout, sku, visit_order, visit_sku, orders, dict(layout_ranks(sku, layout)), tours)
        _CACHE["arrivals"] = {}
    return _CACHE["base"]


def routes_for(scenario, route):
    key = ("routes", scenario, route)
    if key not in _CACHE:
        for k in [k for k in _CACHE if isinstance(k, tuple) and k[0] == "routes"]:
            del _CACHE[k]                        # keep one route set per worker (memory)
        layout, _, visit_order, visit_sku, _, ranks, tours = _inputs()
        if route == "S-shape":
            a, y = m7.pick_coordinates(ranks[scenario], layout, visit_sku)
            visit_lists = [s_shape_visits(oa, oy) for oa, oy in m7.split_orders(visit_order, a, y)]
        else:
            visit_lists = [tour_visits(r[1], r[2], r[3], r[4]) for r in tours[scenario]]
        _CACHE[key] = prepare_routes(visit_lists)
    return _CACHE[key]


def arrivals_for(volume):
    _inputs()
    if volume not in _CACHE["arrivals"]:
        _CACHE["arrivals"][volume] = m8.replicate(_CACHE["base"][4], volume)
    return _CACHE["arrivals"][volume]


def run_config(cfg):
    routes = routes_for(cfg["scenario"], cfg["route"])
    arr = arrivals_for(cfg["volume"])
    shift_end = SHIFT_END_H * 3600
    cap = None if cfg["aisle_model"] == "unlimited" else AISLE_MODELS[cfg["aisle_model"]]
    parts, overtime, ot_work = [], [], []
    aisle_wait = np.zeros(sr.N_AISLES)
    t0 = time.perf_counter()
    for _, day in arr.groupby("date", sort=True):
        res, last, aw = simulate_day(day["t"].to_numpy(), day["base"].to_numpy(), routes,
                                     cfg["pickers"], cap, cfg["policy"])
        res["arrival"] = day["t"].to_numpy()
        parts.append(res)
        # presence (Module 8a definition): each picker from 18:00 to their last completion
        overtime.append(np.maximum(last - shift_end, 0).sum() / 3600)
        # work (used for the service level and cost): picker time actually spent on orders after 18:00
        ot_work.append(np.maximum(res["end"] - np.maximum(res["start"], shift_end), 0).sum() / 3600)
        aisle_wait += aw
    cat = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    a, s, e, w = cat["arrival"], cat["start"], cat["end"], cat["wait"]
    work = e - s
    overtime, ot_work = np.array(overtime), np.array(ot_work)
    days = len(overtime)
    paid_h = cfg["pickers"] * (SHIFT_END_H - SHIFT_START_H) * days + ot_work.sum()
    cost = (cfg["pickers"] * (SHIFT_END_H - SHIFT_START_H) * days + OVERTIME_FACTOR * ot_work.sum()) * WAGE_PER_H
    same_day = 100 * np.mean(e < 86_400)
    ot_ok_days = 100 * np.mean(ot_work / cfg["pickers"] <= SLA_OVERTIME_PER_PICKER_H)
    return {**{k: cfg[k] for k in ("volume", "pickers", "scenario", "route", "aisle_model", "policy")},
            "n_orders": len(a), "days": days,
            "walk_per_order_min": cat["walk"].mean() / 60,
            "aisle_wait_per_order_s": w.mean(),
            "wait_share_of_work_pct": 100 * w.sum() / work.sum(),
            "work_time_per_order_min": work.mean() / 60,
            "queue_wait_min": (s - a).mean() / 60,
            "cycle_time_min": (e - a).mean() / 60,
            "cycle_time_p95_min": np.percentile(e - a, 95) / 60,
            "utilisation_pct": 100 * work.sum() / 3600 / paid_h,
            "overtime_picker_h_per_day": overtime.mean(),
            "overtime_work_picker_h_per_day": ot_work.mean(),
            "orders_same_day_pct": same_day,
            "days_overtime_ok_pct": ot_ok_days,
            "meets_sla": bool(same_day >= SLA_SAME_DAY_PCT and ot_ok_days >= SLA_OVERTIME_DAYS_PCT),
            "cost_units_per_day": cost / days,
            "cost_units_per_order": cost / len(a),
            "skips_per_order": cat["skips"].mean(),
            "front5_wait_share_pct": 100 * aisle_wait[:5].sum() / aisle_wait.sum() if aisle_wait.sum() > 0 else np.nan,
            "runtime_s": time.perf_counter() - t0}


def configs(scope):
    """scope: dict with volumes, scenarios, routes, aisle_models, policies, picker grid."""
    out = []
    for v in scope["volumes"]:
        for n in scope["pickers"][v]:
            for s in scope["scenarios"]:
                for r in scope["routes"]:
                    for am in scope["aisle_models"]:
                        for pol in scope["policies"]:
                            out.append({"volume": v, "pickers": n, "scenario": s, "route": r,
                                        "aisle_model": am, "policy": pol})
    return out


def _run_group(cfgs):
    """Worker task: configurations sharing one (scenario, route), so routes are built once."""
    return [run_config(c) for c in cfgs]


# ----------------------------------------------------------------------------
# Optimal tours for the Spread layouts (Module 7 solver, as in Module 8a)
# ----------------------------------------------------------------------------
def build_spread_tours(n_workers):
    if SPREAD_TOUR_CACHE.exists():
        with open(SPREAD_TOUR_CACHE, "rb") as fh:
            cache = pickle.load(fh)
        if cache.get("meta") == {"gls_solutions": m7.GLS_SOLUTIONS, "gls_max_nodes": m7.GLS_MAX_NODES,
                                 "k": SPREAD_K}:
            print("Spread optimal tours: loaded from cache")
            return
    layout, sku, visit_order, visit_sku, _ = m8.load_orders()
    tours = {}
    with Pool(n_workers) as pool:
        for scenario, ranks in layout_ranks(sku, layout)[4:]:
            t = time.perf_counter()
            a, y = m7.pick_coordinates(ranks, layout, visit_sku)
            orders = m7.split_orders(visit_order, a, y)
            sizes = np.array([len(o[0]) for o in orders])
            chunks, cur, work = [], [], 0
            for i in np.argsort(-sizes, kind="stable"):
                cur.append((int(i), *orders[i]))
                work += max(sizes[i], 10) ** 2
                if work >= 20_000:
                    chunks.append(cur)
                    cur, work = [], 0
            if cur:
                chunks.append(cur)
            res = [None] * len(orders)
            for part in pool.map(m8._tour_chunk, chunks, chunksize=1):
                for i, *rest in part:
                    res[i] = tuple(rest)
            tours[scenario] = res
            print(f"  optimal tours {scenario:<22} {time.perf_counter() - t:6.1f} s", flush=True)
    with open(SPREAD_TOUR_CACHE, "wb") as fh:
        pickle.dump({"meta": {"gls_solutions": m7.GLS_SOLUTIONS, "gls_max_nodes": m7.GLS_MAX_NODES,
                              "k": SPREAD_K}, "tours": tours}, fh)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
FULL_SCOPE = {"volumes": VOLUME_MULTIPLIERS, "pickers": PICKER_GRID, "scenarios": SCENARIOS,
              "routes": ("S-shape", "Optimal"), "aisle_models": tuple(AISLE_MODELS), "policies": POLICIES}


def estimate(n_workers):
    """Time one configuration per volume (Random, S-shape, narrow, wait, smallest and largest
    team) single-process and extrapolate to FULL_SCOPE."""
    samples = {}
    for v in VOLUME_MULTIPLIERS:
        for n in (PICKER_GRID[v][0], PICKER_GRID[v][-1]):
            for pol in POLICIES:
                c = {"volume": v, "pickers": n, "scenario": SCENARIO_VELOCITY, "route": "S-shape",
                     "aisle_model": "narrow", "policy": pol}
                t = time.perf_counter()
                r = run_config(c)
                dt = time.perf_counter() - t
                samples.setdefault(v, []).append(dt)
                print(f"  x{v:<2} {n:>2} pickers {pol:<4}: {dt:6.1f} s | wait {r['aisle_wait_per_order_s']:7.1f} s/order, "
                      f"cycle {r['cycle_time_min']:6.1f} min, skips {r['skips_per_order']:.2f}/order", flush=True)
    per_volume = {v: np.mean(t) for v, t in samples.items()}
    full = configs(FULL_SCOPE)
    serial = sum(per_volume[c["volume"]] for c in full)
    print(f"Full scope: {len(full)} configurations, {serial / 3600:.1f} h single-process, "
          f"~{serial / 60 / 4.5:.0f} min on {n_workers} processes (assumed ~4.5x)")
    return per_volume


# Scope actually run ("core", chosen by the user after the runtime estimate; S-shape only):
#  (1) reference point x10 / 23 pickers: Random and Full velocity, narrow and wide, wait;
#  (2) x10 / 23 pickers, narrow: all 7 layouts x {wait, skip};
#  (3) Class-based ABC and Full velocity, narrow, wait: every volume x PICKER_GRID.
EST_RUNTIME_S = {1: 34, 5: 187, 10: 360}       # single-process seconds per configuration (estimate run)


def core_configs():
    out = []
    ref = {"volume": REFERENCE["volume"], "pickers": REFERENCE["pickers"], "route": "S-shape"}
    for s in (SCENARIO_RANDOM, SCENARIO_VELOCITY):
        for am in AISLE_MODELS:
            out.append({**ref, "scenario": s, "aisle_model": am, "policy": "wait", "set": "reference"})
    for s in SCENARIOS:
        for pol in POLICIES:
            out.append({**ref, "scenario": s, "aisle_model": "narrow", "policy": pol, "set": "spread_skip"})
    for s in (SCENARIO_CLASS, SCENARIO_VELOCITY):
        for v in VOLUME_MULTIPLIERS:
            for n in PICKER_GRID[v]:
                out.append({"volume": v, "pickers": n, "scenario": s, "route": "S-shape",
                            "aisle_model": "narrow", "policy": "wait", "set": "team_size"})
    uniq, seen = [], {}
    for c in out:                                # one run per distinct configuration; keep all set tags
        key = tuple(c[k] for k in ("volume", "pickers", "scenario", "route", "aisle_model", "policy"))
        if key in seen:
            seen[key]["set"] += "+" + c["set"]
        else:
            seen[key] = dict(c)
            uniq.append(seen[key])
    return uniq


def pack_tasks(cfgs, max_s=720):
    """Group configurations of the same layout and route into tasks of <= max_s estimated seconds
    (routes are built once per task), largest tasks first."""
    tasks = []
    for key in sorted({(c["scenario"], c["route"]) for c in cfgs}):
        cur, cost = [], 0
        for c in sorted([c for c in cfgs if (c["scenario"], c["route"]) == key], key=lambda c: -c["volume"]):
            if cur and cost + EST_RUNTIME_S[c["volume"]] > max_s:
                tasks.append((cost, cur))
                cur, cost = [], 0
            cur.append(c)
            cost += EST_RUNTIME_S[c["volume"]]
        tasks.append((cost, cur))
    return [t for _, t in sorted(tasks, key=lambda t: -t[0])]


def team_size_table(res: pd.DataFrame) -> pd.DataFrame:
    """Speed-based (Module 8a elasticity) vs. cost-based (smallest team meeting the service level)
    team size per volume and layout, with the unit cost of each."""
    rows = []
    sub = res[res["set"].str.contains("team_size")]
    for (v, s), g in sub.groupby(["volume", "scenario"]):
        g = g.sort_values("pickers").reset_index(drop=True)
        n, c = g["pickers"].to_numpy(), g["cycle_time_min"].to_numpy()
        el = ((c[1:] - c[:-1]) / c[:-1]) / ((n[1:] - n[:-1]) / n[:-1])
        ok = np.flatnonzero(el > -KNEE_ELASTICITY)
        k_speed = int(ok[0]) if len(ok) else len(n) - 1
        meets = np.flatnonzero(g["meets_sla"].to_numpy())
        k_cost = int(meets[0]) if len(meets) else None
        k_min = int(g["cost_units_per_day"].idxmin())
        rows.append({"volume": v, "scenario": s,
                     "speed_based_pickers": f"{n[k_speed]}" + ("" if len(ok) else f" (>= {n[-1]})"),
                     "speed_based_cycle_min": c[k_speed], "speed_based_cost_per_order": g.loc[k_speed, "cost_units_per_order"],
                     "cost_based_pickers": f"{n[k_cost]}" if k_cost is not None else f"> {n[-1]}",
                     "cost_based_cycle_min": c[k_cost] if k_cost is not None else np.nan,
                     "cost_based_cost_per_order": g.loc[k_cost, "cost_units_per_order"] if k_cost is not None else np.nan,
                     "cost_based_same_day_pct": g.loc[k_cost, "orders_same_day_pct"] if k_cost is not None else np.nan,
                     "cost_based_ot_ok_days_pct": g.loc[k_cost, "days_overtime_ok_pct"] if k_cost is not None else np.nan,
                     "min_cost_pickers_in_grid": int(n[k_min]),
                     "min_cost_per_order": g.loc[k_min, "cost_units_per_order"]})
    return pd.DataFrame(rows)


def strict_vs_refined(res: pd.DataFrame) -> pd.DataFrame:
    """Reference point: Module 8a (whole aisle = 1 picker) next to the segment model."""
    m8a = pd.read_csv(PROC / "congestion_simulation.csv")
    m8a = m8a[(m8a["case"] == "base") & (m8a["volume"] == REFERENCE["volume"])
              & (m8a["pickers"] == REFERENCE["pickers"]) & (m8a["route"] == REFERENCE["route"])]
    rows = []
    for s in (SCENARIO_RANDOM, SCENARIO_VELOCITY):
        a = m8a[m8a["scenario"] == s].iloc[0]
        rows.append({"scenario": s, "model": "8a strict (aisle = 1 picker)", "aisle_wait_per_order_s": a["aisle_wait_per_order_s"],
                     "wait_share_of_work_pct": a["wait_share_of_work_pct"], "cycle_time_min": a["cycle_time_min"],
                     "overtime_picker_h_per_day": a["overtime_picker_h_per_day"]})
        for am in AISLE_MODELS:
            b = res[(res["scenario"] == s) & (res["aisle_model"] == am) & (res["policy"] == "wait")
                    & (res["volume"] == REFERENCE["volume"]) & (res["pickers"] == REFERENCE["pickers"])].iloc[0]
            rows.append({"scenario": s, "model": f"8b {am} (segment = {AISLE_MODELS[am]})",
                         "aisle_wait_per_order_s": b["aisle_wait_per_order_s"],
                         "wait_share_of_work_pct": b["wait_share_of_work_pct"], "cycle_time_min": b["cycle_time_min"],
                         "overtime_picker_h_per_day": b["overtime_picker_h_per_day"]})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------------
def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def short(s):
    return (s.replace(" (baseline)", "").replace("Spread velocity ", "Spread ").replace("Aisle-based velocity", "Aisle-based")
             .replace("Class-based ABC", "ABC"))


def plot_refined(res: pd.DataFrame, svr: pd.DataFrame, path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.6), gridspec_kw={"width_ratios": [1, 1.9]})
    # (a) strict vs refined, aisle wait per order
    models = list(dict.fromkeys(svr["model"]))
    model_colors = ["#52514e", "#2a78d6", "#86b6ef"]
    width = 0.26
    for i, m in enumerate(models):
        sub = svr[svr["model"] == m]
        xs = np.arange(len(sub)) + (i - 1) * width
        vals = sub["aisle_wait_per_order_s"].to_numpy() / 60
        ax1.bar(xs, vals, width - 0.03, color=model_colors[i], label=m, zorder=2)
        for x, v in zip(xs, vals):
            ax1.text(x, v + 0.2, f"{v:.1f}", ha="center", va="bottom", fontsize=8.5, color=INK)
    ax1.set_xticks(range(2))
    ax1.set_xticklabels([short(s) for s in svr["scenario"].unique()], color=INK, fontsize=10)
    ax1.set_ylabel("Aisle wait per order (min)", color=INK_MUTED)
    ax1.set_title("(a) Strict vs. segment model\nx10 volume, 23 pickers, S-shape, wait", loc="left", fontsize=10.5, color=INK)
    ax1.legend(frameon=False, fontsize=8.5, loc="upper left")
    ax1.set_ylim(0, svr["aisle_wait_per_order_s"].max() / 60 * 1.35)
    _style(ax1)
    # (b) cycle time by layout, wait vs skip, narrow segments
    sub = res[res["set"].str.contains("spread_skip")]
    order = SCENARIOS
    xs = np.arange(len(order))
    for i, (pol, col) in enumerate((("wait", "#2a78d6"), ("skip", "#eb6834"))):
        vals = sub[sub["policy"] == pol].set_index("scenario").reindex(order)["cycle_time_min"].to_numpy()
        ax2.bar(xs + (i - 0.5) * 0.38, vals, 0.35, color=col, label={"wait": "Wait", "skip": "Skip-and-return"}[pol], zorder=2)
        for x, v in zip(xs + (i - 0.5) * 0.38, vals):
            ax2.text(x, v + 2, f"{v:.0f}", ha="center", va="bottom", fontsize=8.5, color=INK)
    ax2.set_xticks(xs)
    ax2.set_xticklabels([short(s).replace(" K=", "\nK=") for s in order], color=INK, fontsize=9.5)
    ax2.set_ylabel("Order cycle time (min)", color=INK_MUTED)
    ax2.set_title("(b) Layout and blocking policy, narrow aisles (segment = 1 picker)\nx10 volume, 23 pickers, S-shape",
                  loc="left", fontsize=10.5, color=INK)
    ax2.legend(frameon=False, fontsize=9, loc="upper right")
    ax2.set_ylim(0, sub["cycle_time_min"].max() * 1.18)
    _style(ax2)
    fig.text(0.01, -0.03, "Segment model: each 50 m aisle split into 10 segments of 5 m; a picker holds the segment they are in. "
                          "Cycle time = order arrival to completion. All orders of the last 12 months, copied x10 (+-30 min).",
             fontsize=8, color=INK_MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


LINKEDIN_TITLE = "A too-crude aisle model\npicks the wrong layout"
LINKEDIN_SUBTITLE = "Hours from order to picked, 10x volume, 23 pickers"


def plot_linkedin(res: pd.DataFrame, path: Path) -> None:
    """One message: the strict model (whole aisle = 1 picker) says velocity slotting is worse than
    random at high volume; with 5 m aisle segments it is clearly better."""
    svr = strict_vs_refined(res)
    models = ["8a strict (aisle = 1 picker)", "8b narrow (segment = 1)"]
    group_labels = ["Whole aisle =\n1 picker", "5 m segments,\n1 picker each"]
    layouts = [(SCENARIO_RANDOM, "Random", "#c3c2b7"), (SCENARIO_VELOCITY, "Fast movers\nup front", "#1baf7a")]
    fig, ax = plt.subplots(figsize=(6, 6))
    width = 0.36
    for j, (scen, label, col) in enumerate(layouts):
        vals = [svr[(svr["scenario"] == scen) & (svr["model"] == m)]["cycle_time_min"].iloc[0] / 60 for m in models]
        xs = np.arange(len(models)) + (j - 0.5) * width
        ax.bar(xs, vals, width - 0.04, color=col, zorder=2)
        for x, v in zip(xs, vals):
            ax.text(x, v + 0.06, f"{v:.1f} h", ha="center", va="bottom", fontsize=15, color=INK)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(group_labels, fontsize=13, color=INK)
    ax.set_yticks([])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ymax = svr["cycle_time_min"].max() / 60
    ax.set_ylim(0, ymax * 1.25)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in layouts]
    ax.legend(handles, [l.replace("\n", " ") for _, l, _ in layouts], frameon=False, fontsize=13,
              loc="upper right", bbox_to_anchor=(1.0, 1.02))
    fig.suptitle(LINKEDIN_TITLE, x=0.04, y=0.97, ha="left", va="top", fontsize=20, color=INK, fontweight="bold")
    fig.text(0.04, 0.80, LINKEDIN_SUBTITLE, fontsize=12, color=INK_MUTED, va="top")
    fig.text(0.04, 0.015, "Simulation: 40-aisle warehouse, 1 year of real orders x10, S-shape routing",
             fontsize=8.5, color=INK_MUTED)
    fig.subplots_adjust(left=0.06, right=0.96, top=0.72, bottom=0.15)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--plot-only", action="store_true")
    ap.add_argument("--rerun-team-size", action="store_true")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    if args.estimate:
        estimate(args.workers)
        return
    if args.rerun_team_size:
        rerun_team_size(args.workers)
        return
    if args.plot_only:
        res = pd.read_csv(PROC / "congestion_refined.csv")
        plot_refined(res, strict_vs_refined(res), FIG / "10_congestion_refined.png")
        plot_linkedin(res, FIG / "linkedin_congestion_refined.png")
        return

    t0 = time.perf_counter()
    cfgs = core_configs()
    tasks = pack_tasks(cfgs)
    est = sum(EST_RUNTIME_S[c["volume"]] for c in cfgs)
    print(f"Core scope: {len(cfgs)} configurations in {len(tasks)} tasks, estimated {est / 3600:.2f} h "
          f"single-process (~{est / 60 / 4.5:.0f} min on {args.workers} processes)", flush=True)
    rows = []
    with Pool(args.workers) as pool:
        for part in pool.imap_unordered(_run_group, tasks):
            rows.extend(part)
            last = part[-1]
            print(f"  done {len(rows):>2}/{len(cfgs)}  ({(time.perf_counter() - t0) / 60:5.1f} min)  last: "
                  f"x{last['volume']} {last['pickers']} {short(last['scenario'])} {last['aisle_model']} {last['policy']}: "
                  f"wait {last['aisle_wait_per_order_s']:.0f} s, cycle {last['cycle_time_min']:.0f} min", flush=True)
    res = pd.DataFrame(rows)
    keys = ["volume", "pickers", "scenario", "route", "aisle_model", "policy"]
    tags = {tuple(c[k] for k in keys): c["set"] for c in cfgs}
    res.insert(0, "set", [tags[tuple(r[k] for k in keys)] for _, r in res.iterrows()])
    res = res.sort_values(["set", "volume", "scenario", "aisle_model", "policy", "pickers"]).reset_index(drop=True)
    res.to_csv(PROC / "congestion_refined.csv", index=False)
    print(f"Simulation: {(time.perf_counter() - t0) / 60:.1f} min")
    report(res, t0)


SLA_COST_COLS = ["overtime_work_picker_h_per_day", "days_overtime_ok_pct", "meets_sla",
                 "cost_units_per_day", "cost_units_per_order", "utilisation_pct"]


def rerun_team_size(n_workers):
    """Re-run only the team-size set after the overtime definition was corrected (service level and
    cost now use overtime WORK, not presence). The simulation itself is unchanged, so every
    queueing metric must reproduce the first run exactly; that is asserted. Rows outside the set
    keep their queueing metrics; their service-level / cost columns are cleared (old definition)."""
    t0 = time.perf_counter()
    old = pd.read_csv(PROC / "congestion_refined.csv")
    keys = ["volume", "pickers", "scenario", "route", "aisle_model", "policy"]
    cfgs = [c for c in core_configs() if "team_size" in c["set"]]
    tasks = pack_tasks(cfgs)
    print(f"Re-running the team-size set: {len(cfgs)} configurations in {len(tasks)} tasks", flush=True)
    rows = []
    with Pool(n_workers) as pool:
        for part in pool.imap_unordered(_run_group, tasks):
            rows.extend(part)
            print(f"  done {len(rows):>2}/{len(cfgs)}  ({(time.perf_counter() - t0) / 60:5.1f} min)", flush=True)
    new = pd.DataFrame(rows)
    same = ["walk_per_order_min", "aisle_wait_per_order_s", "wait_share_of_work_pct", "work_time_per_order_min",
            "queue_wait_min", "cycle_time_min", "cycle_time_p95_min", "overtime_picker_h_per_day",
            "orders_same_day_pct", "skips_per_order"]
    chk = new.merge(old, on=keys, suffixes=("", "_old"))
    assert len(chk) == len(new)
    diff = max(float((chk[c] - chk[c + "_old"]).abs().max()) for c in same)
    if diff > 1e-9:
        raise AssertionError(f"re-run differs from the first run (max |diff| {diff})")
    print(f"Check OK: all {len(same)} queueing metrics of the {len(new)} re-run configurations are identical "
          f"to the first run (max |diff| {diff:.1e})")
    tags = {tuple(c[k] for k in keys): c["set"] for c in core_configs()}
    new.insert(0, "set", [tags[tuple(r[k] for k in keys)] for _, r in new.iterrows()])
    rest = old[~old.set_index(keys).index.isin(new.set_index(keys).index)].copy()
    for c in SLA_COST_COLS:
        rest[c] = np.nan
    res = pd.concat([rest, new[rest.columns]], ignore_index=True)
    res = res.sort_values(["set", "volume", "scenario", "aisle_model", "policy", "pickers"]).reset_index(drop=True)
    res.to_csv(PROC / "congestion_refined.csv", index=False)
    print(f"Re-run: {(time.perf_counter() - t0) / 60:.1f} min")
    report(res, t0)


def report(res, t0):
    pd.set_option("display.width", 250)
    fmt = lambda v: f"{v:,.2f}"
    cols = ["volume", "pickers", "scenario", "aisle_model", "policy", "walk_per_order_min", "aisle_wait_per_order_s",
            "wait_share_of_work_pct", "work_time_per_order_min", "queue_wait_min", "cycle_time_min",
            "utilisation_pct", "overtime_picker_h_per_day", "overtime_work_picker_h_per_day",
            "orders_same_day_pct", "days_overtime_ok_pct",
            "meets_sla", "cost_units_per_order", "skips_per_order", "front5_wait_share_pct"]
    print("\n--- ALL RESULTS ---")
    print(res[cols].to_string(index=False, float_format=fmt))
    svr = strict_vs_refined(res)
    print("\n--- STRICT (8a) VS. SEGMENT MODEL (8b), x10 / 23 pickers / S-shape / wait ---")
    print(svr.to_string(index=False, float_format=fmt))
    sp = res[res["set"].str.contains("spread_skip")].pivot_table(
        index="scenario", columns="policy", values=["walk_per_order_min", "aisle_wait_per_order_s", "cycle_time_min"]).reindex(SCENARIOS)
    print("\n--- SPREAD VELOCITY AND SKIP-AND-RETURN, x10 / 23 pickers / narrow / S-shape ---")
    print(sp.to_string(float_format=fmt))
    teams = team_size_table(res)
    print("\n--- TEAM SIZE: SPEED-BASED (elasticity > -%.2f) VS. COST-BASED (service level) ---" % KNEE_ELASTICITY)
    print(teams.to_string(index=False, float_format=fmt))

    plot_refined(res, svr, FIG / "10_congestion_refined.png")
    plot_linkedin(res, FIG / "linkedin_congestion_refined.png")
    print(f"\nTotal runtime: {(time.perf_counter() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
