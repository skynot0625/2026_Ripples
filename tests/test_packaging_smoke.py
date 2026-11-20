"""Packaging smoke test (new in the shareable snapshot; not copied from the research repository).

Checks that the package and every shipped module import, and that ``snnrc`` is loaded
from this project's ``src/`` rather than from any other checkout.
"""

import importlib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
MODULES = sorted(
    ".".join(p.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
    for p in (SRC / "snnrc").rglob("*.py")
)


def test_snnrc_is_imported_from_this_project():
    import snnrc

    assert Path(snnrc.__file__).resolve().is_relative_to(SRC)
    assert snnrc.__version__ == "0.1.0"


@pytest.mark.parametrize("name", MODULES)
def test_every_shipped_module_imports(name):
    mod = importlib.import_module(name)
    assert Path(mod.__file__).resolve().is_relative_to(SRC)
