"""One sweep condition in one OS process (invoked by :mod:`snnrc.sweep.runner`).

Writes only into ``--run-dir`` (a private temporary directory chosen by the
runner).  It never touches the final results directory; finalisation (marker +
atomic rename) is done by the parent after a zero exit status.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import resource
except ImportError:  # Windows has no getrusage.
    resource = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--family", required=True)
    ap.add_argument("--g", required=True, type=float)
    ap.add_argument("--seed", required=True, type=int)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--duration-ms", type=float, default=None)
    ap.add_argument("--j-i", type=float, default=None, help="second control value (2D grids only)")
    a = ap.parse_args(argv)

    import torch

    torch.set_num_threads(1)
    from ..criticality.binned_analysis import analyze_binned_run
    from ..criticality.simulate import run_avalanche_simulation
    from ..plotting import raster_plots
    from ..provenance import write_json
    from .manifest import Condition, build_run_config, control_param, fit_kwargs, load_manifest

    m = load_manifest(a.manifest)
    dec = int(m["grid"].get("decimals", 1))
    cond = Condition(a.family, round(a.g, dec), a.seed, control_param(m), None if a.j_i is None else round(a.j_i, dec))
    if os.environ.get("SNNRC_TEST_FAIL_CONDITION") == cond.condition_id:   # test hook (failure injection)
        raise RuntimeError(f"injected failure for {cond.condition_id}")
    cfg = build_run_config(m, cond, a.duration_ms)
    t0, c0 = time.time(), time.process_time()
    result = run_avalanche_simulation(cfg, a.run_dir)
    t1, c1 = time.time(), time.process_time()
    analyze_binned_run(a.run_dir, fit_kwargs(m), make_plots=True)
    raster_plots(a.run_dir, windows_ms=(None, 1000.0))
    t2, c2 = time.time(), time.process_time()
    write_json({"condition_id": cond.condition_id, "family": cond.family, "g_global": cond.g_global, "seed": cond.seed,
                "control_parameter": cond.param, "control_value": cond.control_value, "J_I": cond.J_I,
                "run_status": result["metrics"]["run_status"],
                "simulation_wall_s": t1 - t0, "simulation_cpu_s": c1 - c0,
                "analysis_wall_s": t2 - t1, "analysis_cpu_s": c2 - c1,
                "max_rss_MB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0 if resource else None,
                "torch_threads": torch.get_num_threads(), "pid": os.getpid()},
               a.run_dir / "timing.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
