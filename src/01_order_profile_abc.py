"""
Module 1 - Order Profile & Velocity-Based ABC Classification

Data : UCI Online Retail II -> data/raw/online_retail_II.xlsx
Run  : python src/01_order_profile_abc.py   (from repo root)
Output:
  reports/figures/01_lines_per_order.png
  reports/figures/02_pick_pareto.png
  data/processed/sku_velocity.csv   (SKU-level pick frequency & ABC classes)
  data/processed/clean_lines.csv    (cleaned order lines, input for Module 2)
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "online_retail_II.xlsx"
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"
PROC.mkdir(parents=True, exist_ok=True)
FIG.mkdir(parents=True, exist_ok=True)

A_CUTOFF, B_CUTOFF = 0.80, 0.95  # cumulative share thresholds for ABC


def load_and_clean(path: Path) -> pd.DataFrame:
    """Load the last 12 months and keep only valid product order lines."""
    df = pd.read_excel(path, sheet_name="Year 2010-2011")
    df.columns = ["Invoice", "StockCode", "Description", "Quantity",
                  "InvoiceDate", "Price", "CustomerID", "Country"]
    n_raw = len(df)

    df["Invoice"] = df["Invoice"].astype(str)
    df["StockCode"] = df["StockCode"].astype(str).str.strip().str.upper()
    df = df[~df["Invoice"].str.startswith("C")]                 # cancellations
    df = df[(df["Quantity"] > 0) & (df["Price"] > 0)]           # invalid rows
    df = df[df["StockCode"].str.match(r"^\d{5}[A-Z]{0,2}$")]    # drop POST, DOT, M, fees...
    df = df.dropna(subset=["Description"]).copy()
    df["Revenue"] = df["Quantity"] * df["Price"]

    print(f"Raw rows: {n_raw:,} -> clean order lines: {len(df):,}")
    return df


def order_profile(df: pd.DataFrame) -> None:
    """Warehouse view: one invoice = one picking task, one line = one location visit."""
    orders = df.groupby("Invoice").agg(lines=("StockCode", "nunique"),
                                       units=("Quantity", "sum"))
    print("\n--- ORDER PROFILE ---")
    print(f"Orders                : {len(orders):,}")
    print(f"Lines per order       : mean {orders['lines'].mean():.1f} | median {orders['lines'].median():.0f}")
    print(f"Single-line orders    : {(orders['lines'] == 1).mean():.1%}")
    print(f"Units per line        : median {df['Quantity'].median():.0f}")

    bins = [0, 1, 5, 10, 20, 50, float("inf")]
    labels = ["1", "2-5", "6-10", "11-20", "21-50", "50+"]
    bands = pd.cut(orders["lines"], bins=bins, labels=labels).value_counts().reindex(labels)

    ax = bands.plot(kind="bar", color="#2b6cb0", figsize=(7, 4))
    ax.set(title="Order lines per order", xlabel="Lines per order", ylabel="Number of orders")
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(FIG / "01_lines_per_order.png", dpi=150)
    plt.close()


def abc_class(values: pd.Series) -> pd.Series:
    """Assign A/B/C based on cumulative share of a metric."""
    s = values.sort_values(ascending=False)
    cum_share = s.cumsum() / s.sum()
    classes = cum_share.apply(lambda x: "A" if x <= A_CUTOFF else ("B" if x <= B_CUTOFF else "C"))
    return classes.reindex(values.index)


def sku_velocity(df: pd.DataFrame) -> pd.DataFrame:
    """Pick frequency per SKU and ABC by pick frequency vs. ABC by revenue."""
    sku = df.groupby("StockCode").agg(
        description=("Description", "first"),
        pick_lines=("Invoice", "count"),
        units=("Quantity", "sum"),
        revenue=("Revenue", "sum"),
    ).sort_values("pick_lines", ascending=False)

    sku["abc_pick"] = abc_class(sku["pick_lines"])
    sku["abc_revenue"] = abc_class(sku["revenue"])

    summary = sku.groupby("abc_pick").agg(skus=("pick_lines", "size"), picks=("pick_lines", "sum"))
    summary["share_of_skus"] = (summary["skus"] / len(sku)).map("{:.1%}".format)
    summary["share_of_picks"] = (summary["picks"] / sku["pick_lines"].sum()).map("{:.1%}".format)
    print("\n--- ABC BY PICK FREQUENCY ---")
    print(summary[["skus", "share_of_skus", "share_of_picks"]])

    print("\n--- ABC BY REVENUE (rows) x ABC BY PICK FREQUENCY (columns) ---")
    print(pd.crosstab(sku["abc_revenue"], sku["abc_pick"], margins=True))
    mismatch = (sku["abc_revenue"] != sku["abc_pick"]).mean()
    print(f"\n{mismatch:.1%} of SKUs fall into a DIFFERENT class by revenue than by pick frequency.")

    cum = sku["pick_lines"].cumsum() / sku["pick_lines"].sum()
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(range(1, len(cum) + 1), cum.values * 100, color="#2b6cb0")
    for y in (A_CUTOFF, B_CUTOFF):
        ax.axhline(y * 100, ls="--", c="gray", lw=0.8)
    ax.set(title="Pick frequency Pareto curve", xlabel="SKUs ranked (fast -> slow)",
           ylabel="Cumulative share of picks (%)")
    plt.tight_layout()
    plt.savefig(FIG / "02_pick_pareto.png", dpi=150)
    plt.close()
    return sku


if __name__ == "__main__":
    lines = load_and_clean(DATA)
    order_profile(lines)
    sku = sku_velocity(lines)
    sku.to_csv(PROC / "sku_velocity.csv")
    lines.to_csv(PROC / "clean_lines.csv", index=False)
    print(f"\nFigures -> {FIG}\nTables  -> {PROC}")
