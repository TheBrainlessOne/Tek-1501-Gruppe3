"""
Graph SCD41 vs Comet T6540 recordings made with compare_logger.py.

For every files/compare_*.csv it saves a PNG with both sensors on the same
graphs (with the ideal indoor range shaded) and prints how far the SCD41
is from the Comet, and how much of the time each sensor was in the ideal range:
  - average difference (SCD41 minus Comet): a steady offset you can correct
  - typical spread: how much the difference varies from reading to reading

The first 10 minutes are left out of the numbers (sensor warm-up).

Usage:
    python compare_plot.py
    python compare_plot.py --minutes   # x-axis in minutes
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

DATA_FOLDER = Path(__file__).parent / "files"
WARMUP_S = 10 * 60

# (name in the columns, axis label, unit, Comet accuracy, ideal low, ideal high)
# Change the ideal values here if you use different guidelines.
QUANTITIES = [
    ("CO2_PPM", "CO₂ (ppm)", "ppm", "±(50 ppm + 3 %)", 400, 1000),
    ("Temperature_C", "Temperature (°C)", "°C", "±0.6 °C", 20, 24),
    ("Humidity_Pct", "Humidity (%)", "%", "±2.5 %", 40, 60),
]


def compare_file(path, use_minutes):
    df = pd.read_csv(path)
    for col in df.columns[2:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    scale = 60 if use_minutes else 1
    x = df["Elapsed_Time_s"] / scale
    settled = df[df["Elapsed_Time_s"] >= WARMUP_S]

    print(f"\n{path.name}  ({len(settled)} readings after warm-up)")
    fig, axes = plt.subplots(len(QUANTITIES), 1, figsize=(11, 8), sharex=True)

    for ax, (name, label, unit, comet_acc, low, high) in zip(axes, QUANTITIES):
        scd, comet = f"SCD41_{name}", f"Comet_{name}"
        ax.axhspan(low, high, color="tab:green", alpha=0.08, label=f"Ideal ({low}–{high})")
        ax.axhline(low, color="tab:green", linestyle="--", linewidth=0.8, alpha=0.6)
        ax.axhline(high, color="tab:green", linestyle="--", linewidth=0.8, alpha=0.6)
        ax.plot(x, df[comet], color="black", linewidth=1.2, label="Comet T6540 (reference)")
        ax.plot(x, df[scd], color="tab:orange", linewidth=1.2, label="SCD41")
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.3)

        diff = (settled[scd] - settled[comet]).dropna()
        if len(diff):
            text = f"SCD41 − Comet: average {diff.mean():+.1f} {unit}, spread ±{diff.std():.1f} {unit}"
            ax.set_title(text, fontsize=9, loc="left")
            print(f"  {label:<18} average difference {diff.mean():+7.1f} {unit}   "
                  f"spread ±{diff.std():.1f} {unit}   (Comet accuracy {comet_acc})")
            for sensor, column in [("SCD41", scd), ("Comet", comet)]:
                values = settled[column].dropna()
                if len(values):
                    inside = values.between(low, high).mean() * 100
                    print(f"      {sensor:<6} within ideal range {low}–{high} {unit}: {inside:.0f} % of the time")

        # keep both the data and the ideal band in view
        data = pd.concat([df[scd], df[comet]]).dropna()
        if len(data):
            lo, hi = min(data.min(), low), max(data.max(), high)
            pad = (hi - lo) * 0.08 or 1
            ax.set_ylim(lo - pad, hi + pad)
        ax.legend(loc="best", fontsize=8)

    axes[-1].set_xlabel("Elapsed time (min)" if use_minutes else "Elapsed time (s)")
    fig.suptitle(f"SCD41 vs Comet T6540 – {path.stem}")
    fig.tight_layout()
    out = path.with_suffix(".png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved {out}")


def main():
    use_minutes = "--minutes" in sys.argv[1:]
    files = sorted(DATA_FOLDER.glob("compare_*.csv"))
    if not files:
        print(f"No compare_*.csv files found in {DATA_FOLDER}")
        sys.exit(1)
    for path in files:
        compare_file(path, use_minutes)


if __name__ == "__main__":
    main()
