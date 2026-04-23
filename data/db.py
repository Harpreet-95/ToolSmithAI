import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "toolsmith.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
