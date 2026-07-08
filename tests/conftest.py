from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proptracker import db as db_module  # noqa: E402
from proptracker.watchlist import seed_firms  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "test.db")
    db_module.init_db(connection)
    seed_firms(connection)
    yield connection
    connection.close()
