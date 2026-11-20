"""Plots generated exclusively from saved run files (never from live simulator state)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def raster_plots(run_dir: Path, windows_ms=(None, 20000.0, 5000.0, 1000.0), max_points: int = 400_000) -> None:
    d = Path(run_dir)
    ev = np.load(d / "spike_events.npz")
    pop = np.load(d / "population_activity.npz")
    kicks = np.flatnonzero(pop["kick_indicator"])
    import yaml

    dt = float(yaml.safe_load((d / "config.yaml").read_text())["reservoir"]["dt_ms"])
    T_ms = len(pop["n_E"]) * dt
    (d / "plots").mkdir(exist_ok=True)
    names = {None: "raster_overview", 20000.0: "raster_last_20s", 5000.0: "raster_last_5s", 1000.0: "raster_last_1s"}
    for w in windows_ms:
        t0 = 0.0 if w is None else max(0.0, T_ms - w)
        m = ev["time_ms"] >= t0
        t, n, p = ev["time_ms"][m], ev["neuron_id"][m], ev["population"][m]
        if len(t) > max_points:                      # downsample the picture only, never the data
            idx = np.random.default_rng(0).choice(len(t), max_points, replace=False)
            t, n, p = t[idx], n[idx], p[idx]
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        a1.scatter(t[p == 0], n[p == 0], s=1, c="royalblue", label="E", rasterized=True)
        a1.scatter(t[p == 1], n[p == 1], s=1, c="crimson", label="I", rasterized=True)
        k = kicks[kicks * dt >= t0] * dt
        a1.vlines(k, -10, -2, color="k", lw=0.5, label="kick")
        a1.set_ylabel("neuron id"); a1.legend(loc="upper right", markerscale=5)
        a1.set_title(f"{names[w]} ({t0:.0f}-{T_ms:.0f} ms)")
        s0 = int(t0 / dt)
        tt = np.arange(s0, len(pop["n_E"])) * dt
        a2.plot(tt, pop["n_E"][s0:], c="royalblue", lw=0.5, label="n_E/step")
        a2.plot(tt, pop["n_I"][s0:], c="crimson", lw=0.5, label="n_I/step")
        a2.set_xlabel("time (ms)"); a2.legend(loc="upper right")
        fig.tight_layout(); fig.savefig(d / "plots" / f"{names[w]}.png", dpi=110); plt.close(fig)


def plot_size_distribution(sizes, fit: dict, path: Path, title: str) -> None:
    from ..criticality import powerlaw as pl

    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    sizes = np.asarray(sizes)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.5))
    if len(sizes):
        u, c = np.unique(sizes, return_counts=True)
        a1.loglog(u, c / c.sum(), "o", ms=3, alpha=0.6, label="empirical PMF")
        ccdf = 1 - np.cumsum(c) / c.sum() + c / c.sum()
        a2.loglog(u, ccdf, "o", ms=3, alpha=0.6, label="empirical CCDF")
        if fit.get("status") == "ok":
            xmin = fit["S_min"]; g = fit["gamma_MLE"]
            frac = fit["n_tail"] / len(sizes)
            uu = u[u >= xmin]
            a2.loglog(uu, frac * (1 - pl.pl_cdf(uu - 1, g, xmin)), "r-",
                      label=f"PL MLE g={g:.3f} [{fit['gamma_ci_low']:.3f},{fit['gamma_ci_high']:.3f}]")
            a1.axvspan(xmin, sizes.max(), color="orange", alpha=0.1, label="fitted range")
            a2.axvspan(xmin, sizes.max(), color="orange", alpha=0.1)
            a2.set_title(f"S_min={xmin} n_tail={fit['n_tail']} KS={fit['ks_statistic']:.3f} p={fit['gof_p_value']}")
        ols = fit.get("gamma_OLS_legacy", {}).get("gamma")
        a1.set_title(f"{title}  (gamma_OLS_legacy={ols if ols is None else round(ols, 3)})")
    a1.set_xlabel("size"); a2.set_xlabel("size"); a1.legend(); a2.legend()
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
