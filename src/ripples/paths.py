"""Portable paths for an editable checkout; generated data never enters examples."""
import os
from pathlib import Path

ROOT = Path(os.environ.get("RIPPLES_ROOT", Path(__file__).resolve().parents[2])).resolve()
DATA_DIR = ROOT / "data_led_example"
LAYOUT_DIR = DATA_DIR / "layout"
RUNTIME_DIR = Path(os.environ.get("RIPPLES_RUNTIME", ROOT / "runtime")).resolve()
OUTPUT_DIR = ROOT / "outputs"
