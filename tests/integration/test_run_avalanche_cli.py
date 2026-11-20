"""Regression: ``python -m snnrc.cli.run_avalanche --analyze`` (previously KeyError: 'fits')."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from snnrc.config import save_config
from snnrc.presets import avalanche_historical_pilot

SRC = Path(__file__).resolve().parents[2] / "src"
SUMMARY_KEYS = {"fit_settings", "definitions", "activity", "activity_full_run", "kicks", "n_safety_events"}


def _json_documents(text):
    dec, i, out = json.JSONDecoder(), 0, []
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory):
    # small dt = 0.1 ms network: multi-spike avalanches, so the fit really runs (status 'ok')
    cfg = avalanche_historical_pilot("B1", g_global=7.0, master_seed=0, duration_ms=1500.0)
    cfg.reservoir.N_E, cfg.reservoir.N_I = 80, 20
    cfg.burn_in_ms = 500.0
    root = tmp_path_factory.mktemp("cli")
    save_config(cfg.validate(), root / "config.yaml")
    out = root / "run"
    cmd = [sys.executable, "-m", "snnrc.cli.run_avalanche", "--config", str(root / "config.yaml"), "--out", str(out),
           "--analyze", "--n-boot-ci", "3", "--n-boot-gof", "3", "--min-tail", "5", "--min-distinct", "3"]
    env = {**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=600)
    return proc, out


def test_analyze_exits_zero(cli_run):
    proc, _ = cli_run
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "KeyError" not in proc.stderr


def test_analysis_outputs_written_and_schema_preserved(cli_run):
    _, out = cli_run
    summ = json.loads((out / "analysis_summary.json").read_text())
    assert set(summ) == SUMMARY_KEYS
    assert summ["fit_settings"]["n_boot_ci"] == 3 and summ["fit_settings"]["min_tail"] == 5
    for name in summ["definitions"]:
        assert (out / f"fit_size_{name}.json").exists() and (out / f"fit_duration_{name}.json").exists()


def test_fitting_not_skipped(cli_run):
    _, out = cli_run
    fit = json.loads((out / "fit_size_primary_E_seg_E_size.json").read_text())
    assert fit["status"] == "ok" and fit["gamma_MLE"] is not None
    assert fit["bootstrap_count_ci"] == 3 and fit["bootstrap_count_gof"] == 3


def test_printed_fits_match_analysis_summary(cli_run):
    proc, out = cli_run
    docs = _json_documents(proc.stdout)
    assert len(docs) == 2                         # analysis-interval activity, then per-definition fits
    printed = docs[1]
    summ = json.loads((out / "analysis_summary.json").read_text())
    assert set(printed) == set(summ["definitions"])
    for name, entry in summ["definitions"].items():
        if entry.get("status") == "no_avalanches":
            assert printed[name] == {"status": "no_avalanches"}
            continue
        for key, fields in (("size_fit", ("status", "validity", "gamma_MLE", "S_min", "n_tail")),
                            ("duration_fit", ("status", "validity", "alpha_MLE", "D_min_steps", "n_tail"))):
            assert {f: printed[name][key][f] for f in fields} == {f: entry[key][f] for f in fields}
