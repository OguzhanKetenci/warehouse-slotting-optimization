"""
Module 8a - Multi-picker Congestion Simulation (SimPy discrete-event simulation)

Question: when several pickers work at the same time, how much time is lost to aisle congestion,
how does that change the slotting and routing conclusions of Modules 2-7, and how many pickers
are needed?

Model (one independent simulation per calendar day; see docs/methodology.md)
  - Orders enter a FIFO queue at their real InvoiceDate time (earliest line of the invoice).
    An idle picker pulls the next order (pull system). Pickers are present from SHIFT_START.
  - A picker leaves the depot, walks the order's route at WALK_SPEED_M_S, spends PICK_TIME_S per
    SKU, returns to the depot; ORDER_HANDLING_S per order covers set-up and hand-over.
  - Every aisle is a capacity-limited resource: to enter, a picker requests a place; if the aisle
    is full they WAIT at the aisle end until a place frees up. The place is held from entering
    the aisle until leaving it at either end. Cross aisles and the depot have no capacity limit.
    A picker never holds more than one aisle, so the model cannot deadlock.
  - Work left in the queue at SHIFT_END is finished; a picker's time after SHIFT_END until their
    last order is complete counts as overtime.
Layouts: Random, Class-based ABC (Module 2, seed 0), Full velocity (Module 2), Aisle-based
velocity (Module 5). Routes: S-shape (Module 2's route, walked aisle by aisle) and Optimal (the
visiting sequence from Module 7's OR-Tools solver, cached on disk).

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv,
        data/processed/optimal_routing_orders.csv.gz (Module 7, used to check the cached tours)
Run   : python src/08_congestion_simulation.py              (full run)
        python src/08_congestion_simulation.py --estimate   (runtime estimate only, writes nothing
                                                              except the optimal-tour cache)
Output:
  data/processed/optimal_tours.pkl              (cache of Module 7 optimal visiting sequences)
  data/processed/congestion_simulation.csv      (one row per simulated configuration)
  reports/figures/09_congestion.png
  reports/figures/linkedin_congestion.png
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


sr = load_module("02_slotting_routing.py", "slotting_routing")
m5 = load_module("05_routing_sensitivity.py", "routing_sensitivity")
m7 = load_module("07_optimal_routing.py", "optimal_routing")
L = sr.AISLE_LENGTH_M

# ----------------------------------------------------------------------------
# Parameters (defaults; sensitivity values in SENSITIVITY)
# ----------------------------------------------------------------------------
WALK_SPEED_M_S = 1.0
PICK_TIME_S = 10.0                 # per SKU (sensitivity: 15)
ORDER_HANDLING_S = 60.0            # per order: set-up + hand-over at the depot
AISLE_CAPACITY = 1                 # pickers allowed in one aisle at the same time (sensitivity: 2)
SHIFT_START_H, SHIFT_END_H = 8, 18 # 98.1% of orders arrive between 08:00 and 18:00 (see methodology)
VOLUME_MULTIPLIERS = (1, 5, 10)
REPLICATION_SHIFT_MIN = 30         # copies of an order arrive uniformly within +-30 min, same day
REPLICATION_SEED = 8
LAYOUT_SEED = 0                    # Random and Class-based ABC: one seed (see methodology)
ROUTES = ("S-shape", "Optimal")
# Picker counts per volume (5 each, to fit the runtime budget - see methodology): from the lowest
# count at which the best layout keeps up on an average day (workload check, step 0) to well past
# the point where extra pickers stop helping.
PICKER_GRID = {1: (2, 3, 4, 6, 8),
               5: (8, 10, 12, 16, 24),
               10: (15, 18, 23, 30, 40)}
SENSITIVITY = {"pick_time_15s": {"pick_time_s": 15.0}, "aisle_capacity_2": {"capacity": 2}}
# Sensitivity runs (runtime budget): x10 volume only, one team size fixed before any results were
# seen (23 = middle of the x10 grid, about what Random/S-shape needs on an average day), Random
# and Full velocity, both routes.
SENSITIVITY_VOLUME = 10
SENSITIVITY_PICKERS = 23
KNEE_ELASTICITY = 0.25             # "optimal" picker count: see choose_optimal_pickers
FRONT_AISLES = 5                   # "front" = the 5 aisles nearest the depot (aisles 1-5)

INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
LAYOUT_COLORS = {sr.SCENARIO_RANDOM: "#2a78d6", sr.SCENARIO_CLASS: "#eb6834",
                 sr.SCENARIO_VELOCITY: "#1baf7a", m5.SCENARIO_AISLE: "#eda100"}
TOUR_CACHE = PROC / "optimal_tours.pkl"


# ----------------------------------------------------------------------------
# Orders
# ----------------------------------------------------------------------------
def load_orders():
    """Base orders in Module 2's order index: arrival time and the slotting inputs."""
    layout = sr.build_layout()
    sku, visit_order, visit_sku = sr.load_inputs()
    lines = pd.read_csv(PROC / "clean_lines.csv", usecols=["Invoice", "StockCode", "InvoiceDate"], dtype=str)
    visits = lines.drop_duplicates(["Invoice", "StockCode"])
    if not (pd.factorize(visits["Invoice"])[0] == visit_order).all():
        raise AssertionError("order index differs from Module 2's load_inputs()")
    first_line = pd.to_datetime(lines.groupby("Invoice", sort=False)["InvoiceDate"].min())
    arrival = first_line.reindex(pd.unique(visits["Invoice"]))
    orders = pd.DataFrame({"invoice": arrival.index, "arrival": arrival.to_numpy(),
                           "n_skus": np.bincount(visit_order)})
    return layout, sku, visit_order, visit_sku, orders


def replicate(orders: pd.DataFrame, k: int, seed: int = REPLICATION_SEED) -> pd.DataFrame:
    """k copies of every order. Copy 0 keeps the real time; copies 1..k-1 are shifted by a uniform
    random offset in +-REPLICATION_SHIFT_MIN minutes, clipped so they stay on the same date.
    Returns columns base (index of the base order), date, t (seconds after midnight), sorted FIFO."""
    rng = np.random.default_rng(seed)
    base = np.repeat(np.arange(len(orders)), k)
    copy = np.tile(np.arange(k), len(orders))
    arrival = orders["arrival"].to_numpy()[base]
    t = (pd.DatetimeIndex(arrival) - pd.DatetimeIndex(arrival).normalize()).total_seconds().to_numpy()
    shift = rng.uniform(-REPLICATION_SHIFT_MIN * 60, REPLICATION_SHIFT_MIN * 60, len(base))
    t = np.where(copy == 0, t, np.clip(t + shift, 0, 86_399))
    out = pd.DataFrame({"base": base, "copy": copy, "date": pd.DatetimeIndex(arrival).normalize(), "t": t})
    return out.sort_values(["date", "t", "base", "copy"], kind="mergesort").reset_index(drop=True)


# ----------------------------------------------------------------------------
# Routes -> aisle visits
# A route is a list of visits (cross_m, aisle, in_aisle_m, n_picks) plus a final cross_m back
# to the depot: walk cross_m along a cross aisle (no capacity), enter `aisle` at one end, walk
# in_aisle_m inside it while picking n_picks SKUs, leave it at one end.
# ----------------------------------------------------------------------------
def s_shape_route(aisles, ys):
    """Module 2's S-shape route: aisles left to right, traversed alternately front->back and
    back->front; with an odd number of aisles the last one is entered from the front and left
    from its farthest pick. Walking length equals sr.s_shape_distance exactly."""
    aisles, ys = np.asarray(aisles), np.asarray(ys, dtype=float)
    uniq = np.unique(aisles)
    odd = len(uniq) % 2
    visits, x_prev = [], 0.0
    for i, a in enumerate(uniq):
        in_aisle = aisles == a
        x = float(m7.aisle_x(a))
        walk = 2 * ys[in_aisle].max() if (odd and i == len(uniq) - 1) else L
        visits.append((x - x_prev, int(a), walk, int(in_aisle.sum())))
        x_prev = x
    return visits, x_prev


def tour_route(loc_aisle, loc_y, loc_count, tour):
    """Walk a closed tour of pick locations (tour = node indices into loc, 0 = depot) along
    shortest paths: consecutive locations in one aisle stay inside it; otherwise the picker leaves
    by the nearer end for the pair (front on ties), crosses, and enters the next aisle from the
    same side. Walking length equals Module 7's tour length exactly."""
    visits = []
    cur = None                                   # (aisle, y) of the current location, None at depot
    cross, walk, picks = 0.0, 0.0, 0
    for node in tour[1:-1]:
        a, y, c = int(loc_aisle[node - 1]), float(loc_y[node - 1]), int(loc_count[node - 1])
        if cur is None:                          # depot -> front of aisle a -> up to y
            cross, walk, picks = float(m7.aisle_x(a)), y, c
        elif cur[0] == a:
            walk += abs(y - cur[1])
            picks += c
        else:
            front = cur[1] + y <= 2 * L - cur[1] - y
            walk += cur[1] if front else L - cur[1]
            visits.append((cross, cur[0], walk, picks))
            cross = abs(float(m7.aisle_x(a)) - float(m7.aisle_x(cur[0])))
            walk, picks = (y if front else L - y), c
        cur = (a, y)
    walk += cur[1]                               # leave by the front, back to the depot
    visits.append((cross, cur[0], walk, picks))
    return visits, float(m7.aisle_x(cur[0]))


def route_walk_m(route) -> float:
    visits, final_cross = route
    return sum(v[0] + v[2] for v in visits) + final_cross


# ----------------------------------------------------------------------------
# Optimal tours (Module 7's solver), cached on disk
# ----------------------------------------------------------------------------
def optimal_tour(aisle, y):
    """Same procedure as m7.solve_order (warm start from the better of Combined / Largest gap,
    GLS for <= m7.GLS_MAX_NODES locations), but also returns the visiting sequence."""
    loc, inv, count = np.unique(np.column_stack([aisle, y]), axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    d = m7.distance_matrix(loc[:, 0].astype(int), loc[:, 1])
    start = None
    for seq_fn in (m7.combined_sequence, m7.largest_gap_sequence):
        nodes = list(dict.fromkeys((inv[seq_fn(aisle, y)] + 1).tolist()))
        length = m7.tour_length(d, [0] + nodes + [0])
        if start is None or length < start[0]:
            start = (length, nodes)
    gls = m7.GLS_SOLUTIONS if len(loc) <= m7.GLS_MAX_NODES else None
    length, tour = m7.solve_tsp(d, initial=start[1], gls_solutions=gls)
    return length, loc[:, 0].astype(np.int16), loc[:, 1].astype(np.float32), count.astype(np.int16), \
        np.array(tour, dtype=np.int16)


def _tour_chunk(chunk):
    return [(i, *optimal_tour(a, y)) for i, a, y in chunk]


def build_tour_cache(layout_runs, visit_order, layout, visit_sku, pool):
    """{scenario: list (per base order) of (length, loc_aisle, loc_y, loc_count, tour)}; reused
    if the cache exists and was built with the same Module 7 search settings."""
    meta = {"gls_solutions": m7.GLS_SOLUTIONS, "gls_max_nodes": m7.GLS_MAX_NODES,
            "layout_seed": LAYOUT_SEED, "n_orders": int(visit_order.max()) + 1}
    if TOUR_CACHE.exists():
        with open(TOUR_CACHE, "rb") as fh:
            cache = pickle.load(fh)
        if cache.get("meta") == meta and all(s in cache["tours"] for s, _ in layout_runs):
            print(f"Optimal tours: loaded from cache {TOUR_CACHE.name}")
            return cache["tours"]
    tours = {}
    for scenario, ranks in layout_runs:
        t = time.perf_counter()
        a, y = m7.pick_coordinates(ranks, layout, visit_sku)
        orders = m7.split_orders(visit_order, a, y)
        sizes = np.array([len(o[0]) for o in orders])
        idx = np.argsort(-sizes, kind="stable")
        chunks, cur, work = [], [], 0
        for i in idx:
            cur.append((int(i), *orders[i]))
            work += max(sizes[i], 10) ** 2
            if work >= 20_000:
                chunks.append(cur)
                cur, work = [], 0
        if cur:
            chunks.append(cur)
        res = [None] * len(orders)
        for part in pool.map(_tour_chunk, chunks, chunksize=1):
            for i, *rest in part:
                res[i] = tuple(rest)
        tours[scenario] = res
        print(f"  optimal tours {scenario:<22} {time.perf_counter() - t:6.1f} s", flush=True)
    with open(TOUR_CACHE, "wb") as fh:
        pickle.dump({"meta": meta, "tours": tours}, fh)
    return tours


def check_tours_against_module7(tours) -> None:
    """Cached tour lengths must equal Module 7's stored Optimal distances (seed 0 / deterministic)."""
    m7_orders = pd.read_csv(PROC / "optimal_routing_orders.csv.gz")
    for scenario, res in tours.items():
        sub = m7_orders[(m7_orders["scenario"] == scenario)
                        & (m7_orders["seed"].isna() | (m7_orders["seed"] == LAYOUT_SEED))].sort_values("order")
        mine = np.array([r[0] for r in res])
        diff = np.abs(mine - sub["Optimal"].to_numpy())
        print(f"  tours vs. Module 7 Optimal, {scenario:<22}: identical in {int((diff < 1e-9).sum()):,} / "
              f"{len(diff):,} orders, max |diff| {diff.max():.2f} m")


# ----------------------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------------------
def build_routes(scenario, ranks, layout, visit_order, visit_sku, tours):
    """{route name: list of routes per base order} for one layout."""
    a, y = m7.pick_coordinates(ranks, layout, visit_sku)
    orders = m7.split_orders(visit_order, a, y)
    return {"S-shape": [s_shape_route(oa, oy) for oa, oy in orders],
            "Optimal": [tour_route(r[1], r[2], r[3], r[4]) for r in tours[scenario]]}


def timed_routes(routes, pick_time_s):
    """Pre-compute times (seconds) for one pick time: per base order a tuple of
    (visits [(cross_s, aisle, in_aisle_s)], final_cross_s, walk_s, pick_s)."""
    out = []
    for visits, final_cross in routes:
        tv = tuple((c / WALK_SPEED_M_S, a, w / WALK_SPEED_M_S + p * pick_time_s) for c, a, w, p in visits)
        walk = (sum(v[0] + v[2] for v in visits) + final_cross) / WALK_SPEED_M_S
        out.append((tv, final_cross / WALK_SPEED_M_S, walk, sum(v[3] for v in visits) * pick_time_s))
    return out


def simulate_day(arrivals, base_idx, routes, n_pickers, capacity, n_aisles=sr.N_AISLES,
                 shift_start_s=SHIFT_START_H * 3600, shift_end_s=SHIFT_END_H * 3600,
                 handling_s=ORDER_HANDLING_S):
    """One day. arrivals: seconds after midnight (FIFO order); base_idx: base order of each;
    routes: timed_routes output. capacity=None -> aisles without capacity limit.
    Returns per-order arrays (start, end, aisle_wait), per-picker last finish, per-aisle wait."""
    env = simpy.Environment()
    queue = simpy.Store(env)
    aisles = None if capacity is None else [simpy.Resource(env, capacity=capacity) for _ in range(n_aisles)]
    n = len(arrivals)
    start, end, wait = np.zeros(n), np.zeros(n), np.zeros(n)
    aisle_wait = np.zeros(n_aisles)
    last_finish = np.zeros(n_pickers)

    def source():
        for i, t in enumerate(arrivals):
            if t > env.now:
                yield env.timeout(t - env.now)
            queue.put(i)

    def picker(p):
        yield env.timeout(shift_start_s)
        while True:
            i = yield queue.get()
            start[i] = env.now
            visits, final_cross, _, _ = routes[base_idx[i]]
            w = 0.0
            pending = handling_s                 # handling time is spent before the first walk
            for cross, a, inside in visits:
                yield env.timeout(pending + cross)
                if aisles is None:
                    pending = inside
                    continue
                res = aisles[a]
                req = res.request()
                if not req.triggered:            # aisle full: wait (a free aisle is granted at once)
                    t0 = env.now
                    yield req
                    dw = env.now - t0
                    w += dw
                    aisle_wait[a] += dw
                yield env.timeout(inside)
                res.release(req)
                pending = 0.0
            yield env.timeout(pending + final_cross)
            end[i], wait[i] = env.now, w
            last_finish[p] = env.now

    env.process(source())
    for p in range(n_pickers):
        env.process(picker(p))
    env.run()
    return start, end, wait, last_finish, aisle_wait


def run_config(cfg):
    """Simulate every day of one configuration; returns a dict of metrics."""
    routes = _ROUTES[(cfg["scenario"], cfg["route"], cfg["pick_time_s"])]
    arr = _ARRIVALS[cfg["volume"]]
    shift_end = SHIFT_END_H * 3600
    rec = {k: [] for k in ("arrival", "start", "end", "wait", "work", "date")}
    overtime, aisle_wait = [], np.zeros(sr.N_AISLES)
    for date, day in arr.groupby("date", sort=True):
        base_idx = day["base"].to_numpy()
        s, e, w, last, aw = simulate_day(day["t"].to_numpy(), base_idx, routes, cfg["pickers"], cfg["capacity"])
        rec["arrival"].append(day["t"].to_numpy())
        rec["start"].append(s)
        rec["end"].append(e)
        rec["wait"].append(w)
        rec["date"].append(np.full(len(day), date.value))
        overtime.append(np.maximum(last - shift_end, 0).sum() / 3600)
        aisle_wait += aw
    a, s, e, w = (np.concatenate(rec[k]) for k in ("arrival", "start", "end", "wait"))
    work = e - s
    no_wait = work - w
    days = len(overtime)
    paid_h = cfg["pickers"] * (SHIFT_END_H - SHIFT_START_H) * days + sum(overtime)
    # completions per hour inside the shift window, averaged over (day, hour)
    dates = np.concatenate(rec["date"])
    hour = (e // 3600).astype(int)
    in_shift = (hour >= SHIFT_START_H) & (hour < SHIFT_END_H)
    per_hour = pd.Series(1, index=pd.MultiIndex.from_arrays([dates[in_shift], hour[in_shift]])).groupby(level=[0, 1]).sum()
    all_hours = pd.MultiIndex.from_product([np.unique(dates), range(SHIFT_START_H, SHIFT_END_H)])
    per_hour = per_hour.reindex(all_hours, fill_value=0)
    total_wait = w.sum()
    return {**{k: cfg[k] for k in ("case", "volume", "pickers", "scenario", "route", "pick_time_s")},
            "aisle_capacity": cfg["capacity"] if cfg["capacity"] is not None else "unlimited",
            "n_orders": len(a), "days": days,
            "aisle_wait_per_order_s": w.mean(),
            "wait_share_of_work_pct": 100 * total_wait / work.sum(),
            "work_time_per_order_min": work.mean() / 60,
            "work_time_no_wait_per_order_min": no_wait.mean() / 60,
            "queue_wait_min": (s - a).mean() / 60,
            "queue_wait_p95_min": np.percentile(s - a, 95) / 60,
            "cycle_time_min": (e - a).mean() / 60,
            "cycle_time_p95_min": np.percentile(e - a, 95) / 60,
            "utilisation_pct": 100 * work.sum() / 3600 / paid_h,
            "orders_per_shift_hour": per_hour.mean(),
            "orders_per_shift_hour_max": per_hour.max(),
            "overtime_picker_h_per_day": sum(overtime) / days,
            "days_with_overtime_pct": 100 * np.mean(np.array(overtime) > 0),
            "front_aisles_wait_share_pct": 100 * aisle_wait[:FRONT_AISLES].sum() / total_wait if total_wait > 0 else np.nan}


_ROUTES, _ARRIVALS = {}, {}


def _init_worker(route_names):
    """Each worker builds its own routes from the tour cache (cheaper than pickling them all)."""
    routes, arrivals = build_inputs(route_names)
    _ROUTES.update(routes)
    _ARRIVALS.update(arrivals)


def configs(volumes=VOLUME_MULTIPLIERS):
    scenarios = [sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE]
    out = []
    for v in volumes:
        for n in PICKER_GRID[v]:
            for s in scenarios:
                for r in ROUTES:
                    out.append({"case": "base", "volume": v, "pickers": n, "scenario": s, "route": r,
                                "pick_time_s": PICK_TIME_S, "capacity": AISLE_CAPACITY})
                    if (v == SENSITIVITY_VOLUME and n == SENSITIVITY_PICKERS
                            and s in (sr.SCENARIO_RANDOM, sr.SCENARIO_VELOCITY)):
                        for case, change in SENSITIVITY.items():
                            out.append({"case": case, "volume": v, "pickers": n, "scenario": s, "route": r,
                                        "pick_time_s": change.get("pick_time_s", PICK_TIME_S),
                                        "capacity": change.get("capacity", AISLE_CAPACITY)})
    return out


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------
def choose_optimal_pickers(res: pd.DataFrame) -> pd.DataFrame:
    """Optimal picker count = the smallest n in the grid at which going to the next grid point no
    longer pays: the elasticity of mean cycle time to staff, (dC / C) / (dn / n), is above
    -KNEE_ELASTICITY (a 10% larger team cuts cycle time by less than 2.5%)."""
    rows = []
    for key, g in res[res["case"] == "base"].groupby(["case", "volume", "scenario", "route"], sort=False):
        g = g.sort_values("pickers")
        n, c = g["pickers"].to_numpy(), g["cycle_time_min"].to_numpy()
        elasticity = ((c[1:] - c[:-1]) / c[:-1]) / ((n[1:] - n[:-1]) / n[:-1])
        ok = np.flatnonzero(elasticity > -KNEE_ELASTICITY)
        k = ok[0] if len(ok) else len(n) - 1
        row = g.iloc[k]
        rows.append({"case": key[0], "volume": key[1], "scenario": key[2], "route": key[3],
                     "optimal_pickers": int(n[k]), "cycle_time_min": row["cycle_time_min"],
                     "aisle_wait_per_order_s": row["aisle_wait_per_order_s"],
                     "wait_share_of_work_pct": row["wait_share_of_work_pct"],
                     "utilisation_pct": row["utilisation_pct"],
                     "overtime_picker_h_per_day": row["overtime_picker_h_per_day"]})
    return pd.DataFrame(rows)


def gains_vs_random(res: pd.DataFrame) -> pd.DataFrame:
    """Per order work time (walk + pick + handling + aisle wait) vs. Random at the same volume,
    picker count and route; with congestion (simulated) and without (the same minus aisle wait)."""
    base = res[res["case"] == "base"]
    rnd = base[base["scenario"] == sr.SCENARIO_RANDOM].set_index(["volume", "pickers", "route"])
    out = base[base["scenario"] != sr.SCENARIO_RANDOM].copy()
    key = pd.MultiIndex.from_frame(out[["volume", "pickers", "route"]])
    r_work = rnd["work_time_per_order_min"].reindex(key).to_numpy()
    r_free = rnd["work_time_no_wait_per_order_min"].reindex(key).to_numpy()
    out["gain_no_congestion_min"] = r_free - out["work_time_no_wait_per_order_min"].to_numpy()
    out["gain_with_congestion_min"] = r_work - out["work_time_per_order_min"].to_numpy()
    out["gain_lost_to_waiting_pct"] = 100 * (1 - out["gain_with_congestion_min"] / out["gain_no_congestion_min"])
    return out[["volume", "pickers", "route", "scenario", "gain_no_congestion_min",
                "gain_with_congestion_min", "gain_lost_to_waiting_pct"]]


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


def plot_congestion(res: pd.DataFrame, path: Path) -> None:
    base = res[res["case"] == "base"]
    scenarios = list(LAYOUT_COLORS)
    fig, axes = plt.subplots(2, len(VOLUME_MULTIPLIERS), figsize=(14, 7.6), sharey="row")
    for row, route in enumerate(ROUTES):
        for col, v in enumerate(VOLUME_MULTIPLIERS):
            ax = axes[row, col]
            for s in scenarios:
                g = base[(base["volume"] == v) & (base["route"] == route) & (base["scenario"] == s)].sort_values("pickers")
                ax.plot(g["pickers"], g["aisle_wait_per_order_s"] / 60, color=LAYOUT_COLORS[s], lw=2,
                        marker="o", ms=4.5, label=s)
            ax.set_xticks(PICKER_GRID[v])
            ax.set_title(f"x{v} volume - {route} routing", color=INK, loc="left", fontsize=10.5)
            if col == 0:
                ax.set_ylabel("Aisle wait per order (min)", color=INK_MUTED)
            if row == 1:
                ax.set_xlabel("Number of pickers", color=INK_MUTED)
            _style(ax)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, fontsize=10, bbox_to_anchor=(0.5, 1.0))
    fig.suptitle("Time lost waiting for a blocked aisle, by slotting scenario, volume and team size",
                 x=0.01, y=1.045, ha="left", fontsize=12, color=INK)
    fig.text(0.01, -0.03, f"Aisle capacity {AISLE_CAPACITY} picker; {PICK_TIME_S:.0f} s per SKU, "
                          f"{ORDER_HANDLING_S:.0f} s per order, 1 m/s. All orders of the last 12 months "
                          "(x5 / x10: each order copied with a random +-30 min shift on the same day).",
             fontsize=8, color=INK_MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_linkedin(res: pd.DataFrame, path: Path, volume: int, route: str) -> None:
    """One message: at high volume, extra pickers only cut order cycle time when fast movers are
    spread out; packed into the front aisles they create a bottleneck no team size can fix."""
    base = res[(res["case"] == "base") & (res["volume"] == volume) & (res["route"] == route)]
    names = {sr.SCENARIO_RANDOM: "Random", sr.SCENARIO_CLASS: "ABC zones",
             sr.SCENARIO_VELOCITY: "Full velocity", m5.SCENARIO_AISLE: "Aisle-by-aisle\nvelocity"}
    fig, ax = plt.subplots(figsize=(6, 6))
    for s in LAYOUT_COLORS:
        g = base[base["scenario"] == s].sort_values("pickers")
        ax.plot(g["pickers"], g["cycle_time_min"] / 60, color=LAYOUT_COLORS[s], lw=3.5, marker="o", ms=7)
        last = g.iloc[-1]
        ax.annotate(names[s], (last["pickers"], last["cycle_time_min"] / 60), xytext=(10, 0),
                    textcoords="offset points", va="center", fontsize=13, color=INK)
    _style(ax)
    ax.set_ylim(0, None)
    ax.set_xticks(PICKER_GRID[volume])
    ax.set_xlabel("Pickers on shift", fontsize=13, color=INK_MUTED)
    ax.set_ylabel("Hours from order to picked", fontsize=13, color=INK_MUTED)
    ax.tick_params(labelsize=12)
    fig.suptitle(LINKEDIN_TITLE, x=0.03, y=0.97, ha="left", va="top", fontsize=19, color=INK, fontweight="bold")
    fig.text(0.03, 0.835, "Fast movers packed into the front aisles create a queue at one aisle",
             fontsize=11.5, color=INK_MUTED)
    fig.text(0.03, 0.015, f"Simulation: 40 aisles, 1 year of real orders x{volume}, {route} routing, "
                          "1 picker per aisle", fontsize=8.5, color=INK_MUTED)
    fig.subplots_adjust(left=0.13, right=0.74, top=0.8, bottom=0.12)
    fig.savefig(path, dpi=200)
    plt.close(fig)


LINKEDIN_TITLE = "More pickers only help\nif fast movers are spread out"
LINKEDIN_VIEW = {"volume": 10, "route": "S-shape"}


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def layout_runs_and_orders():
    layout, sku, visit_order, visit_sku, orders = load_orders()
    runs = [(s, ranks) for s, seed, ranks in m7.layouts(sku, layout, m5, LAYOUT_SEED + 1)
            if seed in (None, LAYOUT_SEED)]
    return layout, visit_order, visit_sku, orders, runs


def build_inputs(route_names=ROUTES):
    """Timed routes for every (layout, route, pick time) and the replicated arrivals per volume.
    The Optimal routes need the tour cache (build_tour_cache) to exist."""
    layout, visit_order, visit_sku, orders, runs = layout_runs_and_orders()
    tours = None
    if "Optimal" in route_names:
        with open(TOUR_CACHE, "rb") as fh:
            tours = pickle.load(fh)["tours"]
    pick_times = {PICK_TIME_S, *[c.get("pick_time_s", PICK_TIME_S) for c in SENSITIVITY.values()]}
    routes = {}
    for scenario, ranks in runs:
        a, y = m7.pick_coordinates(ranks, layout, visit_sku)
        orders_xy = m7.split_orders(visit_order, a, y)
        for route in route_names:
            if route == "S-shape":
                rr = [s_shape_route(oa, oy) for oa, oy in orders_xy]
            else:
                rr = [tour_route(r[1], r[2], r[3], r[4]) for r in tours[scenario]]
            for pt in pick_times:
                routes[(scenario, route, pt)] = timed_routes(rr, pt)
    arrivals = {v: replicate(orders, v) for v in VOLUME_MULTIPLIERS}
    return routes, arrivals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true", help="time a few configurations and exit")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--plot-only", action="store_true", help="redraw the figures from the saved CSV")
    args = ap.parse_args()
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    if args.plot_only:
        res = pd.read_csv(PROC / "congestion_simulation.csv")
        plot_congestion(res, FIG / "09_congestion.png")
        plot_linkedin(res, FIG / "linkedin_congestion.png", **LINKEDIN_VIEW)
        return

    t0 = time.perf_counter()
    if args.estimate:
        routes, arrivals = build_inputs(("S-shape",))
    else:
        layout, visit_order, visit_sku, _, runs = layout_runs_and_orders()
        with Pool(args.workers) as pool:
            tours = build_tour_cache(runs, visit_order, layout, visit_sku, pool)
        check_tours_against_module7(tours)
        del tours
        arrivals = {v: replicate(load_orders()[4], v) for v in VOLUME_MULTIPLIERS}
    print(f"Preparation: {(time.perf_counter() - t0) / 60:.1f} min")
    for v, arr in arrivals.items():
        print(f"  x{v}: {len(arr):,} orders over {arr['date'].nunique()} days")
    cfgs = configs()
    print(f"Configurations: {len(cfgs)} "
          f"({sum(c['case'] == 'base' for c in cfgs)} base + {sum(c['case'] != 'base' for c in cfgs)} sensitivity)")

    if args.estimate:
        sample = [c for c in cfgs if c["case"] == "base" and c["scenario"] == sr.SCENARIO_RANDOM
                  and c["route"] == "S-shape" and c["pickers"] in (min(PICKER_GRID[c["volume"]]),
                                                                    max(PICKER_GRID[c["volume"]]))]
        _ROUTES.update(routes)
        _ARRIVALS.update(arrivals)
        per_volume = {}
        for c in sample:
            t = time.perf_counter()
            run_config(c)
            dt = time.perf_counter() - t
            per_volume.setdefault(c["volume"], []).append(dt)
            print(f"  x{c['volume']:<2} {c['pickers']:>2} pickers: {dt:6.1f} s (single process)")
        serial = sum(np.mean(per_volume[c["volume"]]) for c in cfgs)
        print(f"Estimated simulation time: {serial / 60:.1f} min single-process, "
              f"~{serial / 60 / 4.5:.1f} min on {args.workers} worker processes (assumed ~4.5x speed-up)")
        return

    t1 = time.perf_counter()
    order = sorted(range(len(cfgs)), key=lambda i: -cfgs[i]["volume"] * cfgs[i]["pickers"])
    with Pool(args.workers, initializer=_init_worker, initargs=(ROUTES,)) as pool:
        out = pool.map(run_config, [cfgs[i] for i in order], chunksize=1)
    res = pd.DataFrame(out)
    print(f"Simulation: {(time.perf_counter() - t1) / 60:.1f} min")
    res = res.sort_values(["case", "volume", "route", "scenario", "pickers"]).reset_index(drop=True)
    res.to_csv(PROC / "congestion_simulation.csv", index=False)

    pd.set_option("display.width", 250)
    fmt = lambda v: f"{v:,.2f}"
    cols = ["volume", "pickers", "route", "scenario", "aisle_wait_per_order_s", "wait_share_of_work_pct",
            "work_time_per_order_min", "queue_wait_min", "cycle_time_min", "utilisation_pct",
            "orders_per_shift_hour", "overtime_picker_h_per_day", "front_aisles_wait_share_pct"]
    for case, g in res.groupby("case", sort=False):
        print(f"\n--- RESULTS: {case} ---")
        print(g[cols].to_string(index=False, float_format=fmt))
    opt = choose_optimal_pickers(res)
    print("\n--- OPTIMAL PICKER COUNT (cycle-time elasticity > -%.2f) ---" % KNEE_ELASTICITY)
    print(opt.to_string(index=False, float_format=fmt))
    gains = gains_vs_random(res)
    print("\n--- WORK-TIME GAIN VS. RANDOM (min per order) ---")
    print(gains.to_string(index=False, float_format=fmt))

    plot_congestion(res, FIG / "09_congestion.png")
    plot_linkedin(res, FIG / "linkedin_congestion.png", **LINKEDIN_VIEW)
    print(f"\nTotal runtime: {(time.perf_counter() - t0) / 60:.1f} min")
    print(f"Figures -> {FIG}\nTable  -> {PROC / 'congestion_simulation.csv'}")


if __name__ == "__main__":
    main()
