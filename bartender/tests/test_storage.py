import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bartender.storage import (
    SCHEMA_VERSION,
    SQLiteStateStore,
    StorageConfigurationError,
    create_state_store,
)


def test_sqlite_store_seeds_and_reopens_state(tmp_path):
    database_path = tmp_path / "bartender.db"
    store = SQLiteStateStore(database_path)
    initial = {"settings": {"bar_name": "Test Bar"}, "beers": []}

    store.initialize(initial)
    initial["settings"]["bar_name"] = "Changed after seed"

    reopened = SQLiteStateStore(database_path)
    assert reopened.load({})["settings"]["bar_name"] == "Test Bar"

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == str(SCHEMA_VERSION)


def test_sqlite_store_save_replaces_state_transactionally(tmp_path):
    store = SQLiteStateStore(tmp_path / "bartender.db")
    store.initialize({"version": 1})
    store.save({"version": 2, "beers": [{"id": 1}]})

    assert store.load({}) == {"version": 2, "beers": [{"id": 1}]}


def test_external_storage_requires_deployment_configuration(tmp_path):
    with pytest.raises(StorageConfigurationError, match="DATABASE_URL is required"):
        create_state_store(tmp_path / "bartender.db", "postgresql")

    with pytest.raises(StorageConfigurationError, match="STORAGE_BACKEND"):
        create_state_store(tmp_path / "bartender.db", "oracle")
