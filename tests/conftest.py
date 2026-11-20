import sys
from pathlib import Path

# Never write bytecode into legacy / Chaos source folders while testing.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAOS_SRC = REPO_ROOT / "chaos" / "source_code"
if str(CHAOS_SRC) not in sys.path:
    sys.path.insert(0, str(CHAOS_SRC))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))          # shared test helpers (e.g. speech_fixtures)
