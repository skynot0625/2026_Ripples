"""Run one kick-driven avalanche condition and (optionally) analyse it.

Example::

    python -m snnrc.cli.run_avalanche --config configs/avalanche/example_b0_small_run.yaml \
        --out results/example_run --analyze --n-boot-ci 200 --n-boot-gof 200
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import AvalancheRunConfig, load_config
from ..criticality.analysis import analyze_run
from ..criticality.simulate import run_avalanche_simulation


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--g-global", type=float, default=None, help="override scaling.g_global (global mode only)")
    ap.add_argument("--master-seed", type=int, default=None)
    ap.add_argument("--duration-ms", type=float, default=None)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--n-boot-ci", type=int, default=200)
    ap.add_argument("--n-boot-gof", type=int, default=200)
    ap.add_argument("--min-tail", type=int, default=50)
    ap.add_argument("--min-distinct", type=int, default=10)
    ap.add_argument("--progress", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(AvalancheRunConfig, a.config)
    if a.g_global is not None:
        if cfg.reservoir.scaling.mode != "global":
            raise SystemExit("--g-global requires scaling.mode == 'global'")
        cfg.reservoir.scaling.g_global = a.g_global
    if a.master_seed is not None:
        cfg.reservoir.seeds.master = a.master_seed
    if a.duration_ms is not None:
        cfg.duration_ms = a.duration_ms
    if a.out.exists() and any(a.out.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty {a.out}")
    res = run_avalanche_simulation(cfg, a.out, progress=a.progress)
    print(json.dumps(res["metrics"]["analysis_interval"], indent=1, default=str))
    if a.analyze:
        summ = analyze_run(a.out, n_boot_ci=a.n_boot_ci, n_boot_gof=a.n_boot_gof,
                           min_tail=a.min_tail, min_distinct=a.min_distinct)
        # per-definition fits; a definition without avalanches carries only status='no_avalanches'
        fits = {name: {k: v for k, v in entry.items() if k in ("status", "size_fit", "duration_fit")}
                for name, entry in summ["definitions"].items()}
        print(json.dumps(fits, indent=1, default=str))


if __name__ == "__main__":
    main()
