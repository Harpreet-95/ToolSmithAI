import pathlib
import sqlite3

from core.config import TOOLSMITH_DATA_DIR


def _resolve_db_path(data_dir: str) -> pathlib.Path:
    """TOOLSMITH_DATA_DIR, if set, relocates the sqlite metadata DB outside
    the source tree (e.g. off a OneDrive-synced checkout — see
    core.config.TOOLSMITH_DATA_DIR's docstring). Empty/unset keeps the
    historical default next to this file, unchanged from before this
    setting existed. A pure function of its argument (not a direct
    os.getenv() read) so path resolution is unit-testable without
    reloading this module."""
    base = pathlib.Path(data_dir) if data_dir else pathlib.Path(__file__).parent
    return base / "toolsmith.db"


DB_PATH = _resolve_db_path(TOOLSMITH_DATA_DIR)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
