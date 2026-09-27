"""
Module 4 - Out-of-sample check of the slotting scenarios

Question: Modules 2-3 rank SKUs with the same period they are evaluated on (in-sample).
How much of the saving survives when SKUs are ranked on past orders only, and how much of the
remaining decline is simply new SKUs the ranking never saw versus a shift in pick frequency
among already-known SKUs?

Two independent splits (both use the Module 2 layout, S-shape routing and 10 random seeds)
  - "6+6 months": within the last 12 months (Module 1 data). History = Dec 2010 - May 2011,
    future = Jun - Dec 2011. SKU universe = all 3,791 SKUs of the full year.
  - "year-over-year": history = the whole prior year ('Year 2009-2010' sheet, trimmed to
    1 Dec 2009 - 30 Nov 2010), future = the whole evaluated year ('Year 2010-2011' sheet,
    trimmed to 1 Dec 2010 - 30 Nov 2011). SKU universe = only SKUs picked in the future year
    (a warehouse would not reserve a slot for an item it has never stocked). If that SKU count
    exceeds the default 4,000-slot layout, aisles are added and the run is logged.
    The two sheets share an exact 9-day overlap (1-9 Dec 2010, verified byte-for-byte identical);
    it is detected and removed before the history window is applied.

For each split, ranking_basis is:
  - in_sample     : pick frequency and ABC classes computed from the future/evaluation period itself.
  - out_of_sample : computed from the history period only. SKUs never picked in history get
    frequency 0, class C, and are placed last (ties by SKU code).

Decomposition (out-of-sample only): every out-of-sample scenario is evaluated twice -
  - visit_scope "all_visits"       : every order-SKU visit of the future period (as usual).
  - visit_scope "seen_only_visits" : the same visits, excluding any SKU never picked in history,
    with its own 10-seed random baseline on the same restricted visits.
  "seen_only_visits" isolates the saving that would exist even if every future SKU had already
  been seen in history; the gap between the two scopes is the extra decline caused specifically
  by slotting brand-new SKUs into the layout.

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv, data/raw/online_retail_II.xlsx
Run   : python src/04_out_of_sample_check.py   (from repo root, after Module 1)
Output:
  data/processed/out_of_sample_results.csv
  reports/figures/05_in_vs_out_of_sample.png
"""
import importlib.util
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "online_retail_II.xlsx"
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"

SPLIT_6_6 = "6+6 months"
SPLIT_YOY = "year-over-year"
WITHIN_YEAR_SPLIT_DATE = pd.Timestamp("2011-06-01")             # history: Dec 2010-May 2011, future: Jun-Dec 2011
YOY_HISTORY_RANGE = (pd.Timestamp("2009-12-01"), pd.Timestamp("2010-12-01"))   # [start, end)
YOY_FUTURE_RANGE = (pd.Timestamp("2010-12-01"), pd.Timestamp("2011-12-01"))    # [start, end)

BASIS_NONE, BASIS_IN, BASIS_OUT = "none", "in_sample", "out_of_sample"
SCOPE_ALL, SCOPE_SEEN = "all_visits", "seen_only_visits"
BASIS_LABEL = {BASIS_IN: "In-sample (ranked on the evaluation period)",
               BASIS_OUT: "Out-of-sample (ranked on the earlier period)"}
KEY_COLS = ["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "CustomerID", "Country"]
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# Period splits
# ----------------------------------------------------------------------------
def within_year_periods():
    """6+6 split: Module 1's cleaned lines, history = before the split date, future = on/after."""
    lines = pd.read_csv(PROC / "clean_lines.csv", usecols=["Invoice", "StockCode", "InvoiceDate"],
                        dtype={"Invoice": str, "StockCode": str}, parse_dates=["InvoiceDate"])
    invoice_start = lines.groupby("Invoice")["InvoiceDate"].transform("min")
    history, future = lines[invoice_start < WITHIN_YEAR_SPLIT_DATE], lines[invoice_start >= WITHIN_YEAR_SPLIT_DATE]
    assert not set(history["Invoice"]) & set(future["Invoice"]), "an invoice spans both periods"
    return history, future, {}


def combined_deduped_lines(m1):
    """History sheet ('Year 2009-2010') and Module 1's clean_lines.csv ('Year 2010-2011'), both
    cleaned with Module 1's rules, with cross-sheet duplicate rows removed from the history side.
    Not yet windowed to any date range - Module 6 reuses this to build rolling monthly snapshots
    that span both sheets, in addition to year_over_year_periods()'s own fixed-year windows below.
    """
    future_raw = pd.read_csv(PROC / "clean_lines.csv", usecols=KEY_COLS,
                             dtype={"Invoice": str, "StockCode": str}, parse_dates=["InvoiceDate"])
    history_raw = m1.load_and_clean(DATA, sheet_name="Year 2009-2010")

    # Match the k-th occurrence of an identical row in history to the k-th occurrence in future, so a
    # key that legitimately repeats within one sheet isn't over- or under-matched by a plain key merge
    # (which would multiply rows whenever a key is non-unique on either side).
    h_occ = history_raw.assign(_occ=history_raw.groupby(KEY_COLS, dropna=False).cumcount())
    f_occ = future_raw[KEY_COLS].assign(_occ=future_raw.groupby(KEY_COLS, dropna=False).cumcount())
    merged = h_occ.merge(f_occ, on=KEY_COLS + ["_occ"], how="left", indicator=True)
    assert len(merged) == len(history_raw), "occurrence-matched merge changed row count"
    is_dup = (merged["_merge"] == "both").to_numpy()
    dup_info = {"cross_sheet_duplicate_rows": int(is_dup.sum()),
                "cross_sheet_duplicate_invoices": int(history_raw.loc[is_dup, "Invoice"].nunique())}
    history_raw = history_raw.loc[~is_dup].copy()
    return history_raw, future_raw, dup_info


def year_over_year_periods(m1):
    """Year-over-year split: prior year vs. evaluated year, each trimmed to a full calendar year."""
    history_raw, future_raw, dup_info = combined_deduped_lines(m1)

    def window(df, start, end):
        invoice_start = df.groupby("Invoice")["InvoiceDate"].transform("min")
        return df[(invoice_start >= start) & (invoice_start < end)]

    history = window(history_raw, *YOY_HISTORY_RANGE)
    future = window(future_raw, *YOY_FUTURE_RANGE)
    assert not set(history["Invoice"]) & set(future["Invoice"]), "an invoice spans both periods"
    return history, future, dup_info


# ----------------------------------------------------------------------------
# SKU stats and visits
# ----------------------------------------------------------------------------
def sku_stats(period_lines: pd.DataFrame, codes: list, abc_class):
    """Pick lines and ABC class (Module 1 definition) per SKU from one period."""
    pick_lines = period_lines.groupby("StockCode").size().reindex(codes).fillna(0).astype(int)
    return pick_lines.to_numpy(), abc_class(pick_lines).to_numpy()


def order_visits(period_lines: pd.DataFrame, sku_index: pd.Series):
    """Distinct (order, SKU) visits as integer index arrays."""
    visits = period_lines.drop_duplicates(["Invoice", "StockCode"])
    return pd.factorize(visits["Invoice"])[0], sku_index.reindex(visits["StockCode"]).to_numpy().astype(int)


# ----------------------------------------------------------------------------
# One split: layout sizing, all scenarios, both ranking bases, both visit scopes
# ----------------------------------------------------------------------------
def size_layout(sr, n_skus: int):
    """Default layout if it fits the SKU universe, otherwise the smallest layout (more aisles) that does."""
    default_slots = sr.N_AISLES * sr.SIDES_PER_AISLE * sr.SLOTS_PER_SIDE
    if n_skus <= default_slots:
        return sr.build_layout(), sr.N_AISLES
    n_aisles = math.ceil(n_skus / (sr.SIDES_PER_AISLE * sr.SLOTS_PER_SIDE))
    return sr.build_layout(n_aisles=n_aisles), n_aisles


def run_split(sr, abc_class, split_name: str, history: pd.DataFrame, future: pd.DataFrame, codes: list):
    layout, n_aisles = size_layout(sr, len(codes))
    near_cut = int(round(sr.NEAR_SLOT_SHARE * len(layout)))
    sku_index = pd.Series(np.arange(len(codes)), index=codes)

    vo_all, vs_all = order_visits(future, sku_index)
    history_pick, history_cls = sku_stats(history, codes, abc_class)
    eval_pick, eval_cls = sku_stats(future, codes, abc_class)

    unseen_sku = history_pick == 0                          # SKUs never picked in the history period
    seen_mask = ~unseen_sku[vs_all]
    vo_seen, vs_seen = vo_all[seen_mask], vs_all[seen_mask]
    n_orders_all, n_orders_seen = len(np.unique(vo_all)), len(np.unique(vo_seen))

    rows = []
    def record(scenario, basis, scope, seed, ranks):
        vo, vs = (vo_all, vs_all) if scope == SCOPE_ALL else (vo_seen, vs_seen)
        avg, total_km, near = sr.evaluate(ranks, layout, vo, vs, near_cut)
        rows.append({"split": split_name, "scenario": scenario, "ranking_basis": basis, "visit_scope": scope,
                     "seed": seed, "avg_distance_per_order_m": avg, "total_distance_km": total_km,
                     "share_picks_nearest_20pct_slots": near,
                     "n_orders_evaluated": n_orders_all if scope == SCOPE_ALL else n_orders_seen})

    for seed in sr.RANDOM_SEEDS:
        ranks = sr.slot_random(len(codes), len(layout), np.random.default_rng(seed))
        record(sr.SCENARIO_RANDOM, BASIS_NONE, SCOPE_ALL, seed, ranks)
        record(sr.SCENARIO_RANDOM, BASIS_NONE, SCOPE_SEEN, seed, ranks)

    for basis, pick, cls in ((BASIS_IN, eval_pick, eval_cls), (BASIS_OUT, history_pick, history_cls)):
        for seed in sr.RANDOM_SEEDS:
            ranks = sr.slot_class_based(cls, np.random.default_rng(seed))
            record(sr.SCENARIO_CLASS, basis, SCOPE_ALL, seed, ranks)
            if basis == BASIS_OUT:
                record(sr.SCENARIO_CLASS, basis, SCOPE_SEEN, seed, ranks)
        ranks = sr.slot_full_velocity(pick)
        record(sr.SCENARIO_VELOCITY, basis, SCOPE_ALL, None, ranks)
        if basis == BASIS_OUT:
            record(sr.SCENARIO_VELOCITY, basis, SCOPE_SEEN, None, ranks)

    info = {"split": split_name, "history_orders": history["Invoice"].nunique(),
            "future_orders": n_orders_all, "n_skus": len(codes), "n_aisles": n_aisles,
            "n_slots": len(layout), "evaluation_visits_all": len(vo_all), "evaluation_visits_seen": len(vo_seen),
            "orders_fully_unseen": n_orders_all - n_orders_seen,
            "unseen_skus": int(unseen_sku.sum()), "unseen_visit_share": float((~seen_mask).mean()),
            "abc_counts_history": {c: int((history_cls == c).sum()) for c in sr.CLASS_ORDER},
            "abc_counts_evaluation": {c: int((eval_cls == c).sum()) for c in sr.CLASS_ORDER}}
    return pd.DataFrame(rows), info


# ----------------------------------------------------------------------------
# Summary and decomposition
# ----------------------------------------------------------------------------
def summarize(runs: pd.DataFrame, random_name: str, order: list) -> pd.DataFrame:
    parts = []
    for split in runs["split"].unique():
        for scope in runs["visit_scope"].unique():
            sub = runs[(runs["split"] == split) & (runs["visit_scope"] == scope)]
            if sub.empty:
                continue
            base = sub.loc[sub["scenario"] == random_name, "total_distance_km"].mean()
            sub = sub.assign(change=sub["total_distance_km"] / base - 1)
            g = sub.groupby(["scenario", "ranking_basis"], sort=False)
            part = pd.DataFrame({
                "n_runs": g["total_distance_km"].size(),
                "avg_distance_per_order_m": g["avg_distance_per_order_m"].mean(),
                "avg_distance_per_order_min_m": g["avg_distance_per_order_m"].min(),
                "avg_distance_per_order_max_m": g["avg_distance_per_order_m"].max(),
                "total_distance_km": g["total_distance_km"].mean(),
                "share_picks_nearest_20pct_slots": g["share_picks_nearest_20pct_slots"].mean(),
                "change_vs_random": g["total_distance_km"].mean() / base - 1,
                "change_vs_random_min": g["change"].min(),
                "change_vs_random_max": g["change"].max(),
            }).reset_index()
            part.insert(0, "visit_scope", scope)
            part.insert(0, "split", split)
            parts.append(part)
    out = pd.concat(parts, ignore_index=True)
    key = out.apply(lambda r: order.index((r["split"], r["scenario"], r["ranking_basis"], r["visit_scope"])), axis=1)
    return out.assign(_k=key).sort_values("_k").drop(columns="_k").reset_index(drop=True)


def decomposition_table(results: pd.DataFrame) -> pd.DataFrame:
    """For each split and scenario, split the out-of-sample decline into a new-SKU part and the rest."""
    rows = []
    out = results[(results["ranking_basis"] == BASIS_OUT)]
    for split in out["split"].unique():
        for scenario in out["scenario"].unique():
            sub = out[(out["split"] == split) & (out["scenario"] == scenario)]
            all_v = sub[sub["visit_scope"] == SCOPE_ALL]
            seen_v = sub[sub["visit_scope"] == SCOPE_SEEN]
            if all_v.empty or seen_v.empty:
                continue
            red_all = -all_v["change_vs_random"].iloc[0]
            red_seen = -seen_v["change_vs_random"].iloc[0]
            rows.append({"split": split, "scenario": scenario, "reduction_all_visits_pct": red_all * 100,
                        "reduction_seen_only_visits_pct": red_seen * 100,
                        "new_sku_effect_pp": (red_seen - red_all) * 100})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------------
def _tint(color: str, amount: float = 0.6):
    return tuple(c + (1 - c) * amount for c in to_rgb(color))


def plot_in_vs_out(results: pd.DataFrame, colors: dict, split_info: dict, path: Path) -> None:
    splits = [SPLIT_6_6, SPLIT_YOY]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), sharey=True)
    width = 0.34
    for ax, split in zip(axes, splits):
        sub_all = results[(results["split"] == split) & (results["visit_scope"] == SCOPE_ALL) &
                          (results["ranking_basis"] != BASIS_NONE)]
        scenarios = list(sub_all["scenario"].unique())
        for i, scenario in enumerate(scenarios):
            for j, basis in enumerate((BASIS_IN, BASIS_OUT)):
                row = sub_all[(sub_all["scenario"] == scenario) & (sub_all["ranking_basis"] == basis)].iloc[0]
                value = -row["change_vs_random"] * 100
                x = i + (j - 0.5) * (width + 0.04)
                in_sample = basis == BASIS_IN
                ax.bar(x, value, width, color=_tint(colors[scenario]) if in_sample else colors[scenario],
                       edgecolor=colors[scenario], linewidth=1.2, hatch="///" if in_sample else None, zorder=2)
                if row["n_runs"] > 1:
                    lo, hi = -row["change_vs_random_max"] * 100, -row["change_vs_random_min"] * 100
                    ax.errorbar(x, value, yerr=[[value - lo], [hi - value]], fmt="none", ecolor=INK,
                                elinewidth=1.2, capsize=4, zorder=3)
                    top = hi
                else:
                    top = value
                ax.text(x, top + 0.8, f"{value:.1f}%", ha="center", va="bottom", fontsize=9.5, color=INK)
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(scenarios, color=INK, fontsize=10)
        info = split_info[split]
        ax.set_title(f"{split}  ({info['history_orders']:,} history / {info['future_orders']:,} future orders)",
                    color=INK, loc="left", fontsize=10.5)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=INK_MUTED, length=0)
        ax.yaxis.grid(True, color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Reduction in avg. distance per order vs. random (%)", color=INK_MUTED)
    ymax = max(results.loc[results["visit_scope"] == SCOPE_ALL, "change_vs_random"].abs())
    axes[0].set_ylim(0, ymax * 100 * 1.25)
    axes[1].legend(handles=[Patch(facecolor=_tint("#7a7a7a"), edgecolor="#7a7a7a", hatch="///", label=BASIS_LABEL[BASIS_IN]),
                            Patch(facecolor="#7a7a7a", edgecolor="#7a7a7a", label=BASIS_LABEL[BASIS_OUT])],
                  frameon=False, fontsize=9, loc="upper right")
    fig.suptitle("Slotting savings: in-sample vs. out-of-sample, two ranking/evaluation splits",
                x=0.01, ha="left", color=INK, fontsize=11.5, y=1.02)
    fig.text(0.01, -0.03, "Whiskers = min-max over 10 seeds. Both splits use the same layout and S-shape routing.",
            fontsize=8, color=INK_MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    sr = load_module("02_slotting_routing.py", "slotting_routing")
    m1 = load_module("01_order_profile_abc.py", "order_profile_abc")
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    all_runs, split_info, dup_info = [], {}, {}

    history_6, future_6, dup_6 = within_year_periods()
    codes_6 = sorted(pd.read_csv(PROC / "sku_velocity.csv", dtype={"StockCode": str})["StockCode"])   # full year, as before
    runs_6, info_6 = run_split(sr, m1.abc_class, SPLIT_6_6, history_6, future_6, codes_6)
    all_runs.append(runs_6)
    split_info[SPLIT_6_6] = info_6
    dup_info[SPLIT_6_6] = dup_6

    history_yoy, future_yoy, dup_yoy = year_over_year_periods(m1)
    codes_yoy = sorted(future_yoy["StockCode"].unique())   # only SKUs actually picked in the evaluated year
    runs_yoy, info_yoy = run_split(sr, m1.abc_class, SPLIT_YOY, history_yoy, future_yoy, codes_yoy)
    all_runs.append(runs_yoy)
    split_info[SPLIT_YOY] = info_yoy
    dup_info[SPLIT_YOY] = dup_yoy

    runs = pd.concat(all_runs, ignore_index=True)
    runs["seed"] = runs["seed"].astype("Int64")

    order = []
    for split in (SPLIT_6_6, SPLIT_YOY):
        order.append((split, sr.SCENARIO_RANDOM, BASIS_NONE, SCOPE_ALL))
        order.append((split, sr.SCENARIO_RANDOM, BASIS_NONE, SCOPE_SEEN))
        for scenario in (sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY):
            order.append((split, scenario, BASIS_IN, SCOPE_ALL))
            order.append((split, scenario, BASIS_OUT, SCOPE_ALL))
            order.append((split, scenario, BASIS_OUT, SCOPE_SEEN))
    results = summarize(runs, sr.SCENARIO_RANDOM, order)
    decomposition = decomposition_table(results)

    for split in (SPLIT_6_6, SPLIT_YOY):
        info, dup = split_info[split], dup_info[split]
        print(f"\n=== Split: {split} ===")
        if dup:
            print(f"Cross-sheet duplicate rows removed from history: {dup['cross_sheet_duplicate_rows']:,} "
                  f"({dup['cross_sheet_duplicate_invoices']:,} invoices)")
        print(f"History orders  : {info['history_orders']:,}")
        print(f"Future orders   : {info['future_orders']:,} ({info['evaluation_visits_all']:,} order-SKU visits)")
        print(f"SKU universe    : {info['n_skus']:,} SKUs -> layout {info['n_aisles']} aisles, {info['n_slots']:,} slots")
        print(f"ABC class sizes : history {info['abc_counts_history']} | evaluation {info['abc_counts_evaluation']}")
        print(f"SKUs picked in future but never in history: {info['unseen_skus']:,} "
              f"({info['unseen_visit_share']:.1%} of future visits, {info['orders_fully_unseen']:,} orders "
              f"entirely made of such SKUs and dropped from the seen-only scope)")

    print("\n--- RUNS (one row per split x scenario x ranking basis x visit scope x seed) ---")
    print(runs.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("\n--- OUT-OF-SAMPLE RESULTS ---")
    print(results.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print("\n--- DECOMPOSITION: reduction vs. random, all visits vs. seen-only visits (out-of-sample) ---")
    print(decomposition.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    results.to_csv(PROC / "out_of_sample_results.csv", index=False)
    plot_in_vs_out(results, sr.SCENARIO_COLORS, split_info, FIG / "05_in_vs_out_of_sample.png")
    print(f"\nFigure -> {FIG / '05_in_vs_out_of_sample.png'}\nTable  -> {PROC / 'out_of_sample_results.csv'}")


if __name__ == "__main__":
    main()
