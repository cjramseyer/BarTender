"""Transactional internal storage for BarTender application state."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = 1


class StorageConfigurationError(RuntimeError):
    """Raised when an external storage backend cannot be configured."""


class SQLiteStateStore:
    """Stores the current application document transactionally in SQLite.

    The state payload remains JSON-shaped for compatibility with the existing
    service layer. The SQLite boundary gives writes transactions, locking,
    WAL-backed durability, and a versioned migration point for future
    normalized repositories.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.execute(
            """INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(SCHEMA_VERSION),),
        )

    def initialize(self, initial_state: dict) -> None:
        """Create the database and seed it once when no state exists."""
        payload = json.dumps(initial_state, separators=(",", ":"), ensure_ascii=False)
        with self._connection() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """INSERT INTO app_state(id, payload) VALUES(1, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (payload,),
            )
            connection.commit()

    def load(self, default_state: dict) -> dict:
        self.initialize(default_state)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM app_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return json.loads(json.dumps(default_state))
        return json.loads(row[0])

    def save(self, state: dict) -> None:
        payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        with self._connection() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """INSERT INTO app_state(id, payload, updated_at)
                   VALUES(1, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET
                     payload = excluded.payload,
                     updated_at = excluded.updated_at""",
                (payload,),
            )
            connection.commit()

    def export_state(self) -> dict:
        with self._connection() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT payload FROM app_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return {}
        return json.loads(row[0])


class ExternalStateStore:
    """Stores the same state payload in PostgreSQL or MariaDB.

    External backends are optional and selected only through deployment
    configuration. Credentials never pass through application settings or
    exports.
    """

    def __init__(self, backend: str, database_url: str):
        self.backend = backend
        self.database_url = database_url
        if not database_url:
            raise StorageConfigurationError(
                f"DATABASE_URL is required when STORAGE_BACKEND={backend}."
            )

    def _connect(self):
        if self.backend == "postgresql":
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise StorageConfigurationError(
                    "PostgreSQL storage requires the psycopg package."
                ) from exc
            return psycopg.connect(self.database_url)

        if self.backend == "mariadb":
            try:
                import pymysql  # type: ignore[import-not-found]
            except ImportError as exc:
                raise StorageConfigurationError(
                    "MariaDB storage requires the PyMySQL package."
                ) from exc
            parsed = urlparse(self.database_url)
            if parsed.scheme not in ("mysql", "mariadb") or not parsed.hostname or not parsed.path:
                raise StorageConfigurationError(
                    "MariaDB DATABASE_URL must use mysql:// or mariadb:// with a database name."
                )
            return pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                database=parsed.path.lstrip("/"),
                autocommit=False,
            )

        raise StorageConfigurationError(f"Unsupported storage backend: {self.backend}.")

    def _schema_sql(self) -> tuple[str, str, str]:
        if self.backend == "mariadb":
            return (
                """CREATE TABLE IF NOT EXISTS bartender_schema_meta (
                    `key` VARCHAR(64) PRIMARY KEY,
                    value TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS bartender_app_state (
                    id INTEGER PRIMARY KEY,
                    payload LONGTEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )""",
                "INSERT INTO bartender_schema_meta(`key`, value) VALUES(%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)",
            )
        return (
            """CREATE TABLE IF NOT EXISTS bartender_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS bartender_app_state (
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "INSERT INTO bartender_schema_meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
        )

    def _ensure_schema(self, connection) -> None:
        meta_sql, state_sql, version_sql = self._schema_sql()
        with connection.cursor() as cursor:
            cursor.execute(meta_sql)
            cursor.execute(state_sql)
            cursor.execute(version_sql, ("schema_version", str(SCHEMA_VERSION)))

    def initialize(self, initial_state: dict) -> None:
        payload = json.dumps(initial_state, separators=(",", ":"), ensure_ascii=False)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            with connection.cursor() as cursor:
                if self.backend == "mariadb":
                    cursor.execute(
                        "INSERT IGNORE INTO bartender_app_state(id, payload) VALUES(%s, %s)",
                        (1, payload),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO bartender_app_state(id, payload) VALUES(%s, %s) ON CONFLICT(id) DO NOTHING",
                        (1, payload),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load(self, default_state: dict) -> dict:
        self.initialize(default_state)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM bartender_app_state WHERE id = %s", (1,))
                row = cursor.fetchone()
        finally:
            connection.close()
        if row is None:
            return json.loads(json.dumps(default_state))
        payload = row[0] if not isinstance(row, dict) else row["payload"]
        return json.loads(payload)

    def save(self, state: dict) -> None:
        payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            with connection.cursor() as cursor:
                if self.backend == "mariadb":
                    cursor.execute(
                        """INSERT INTO bartender_app_state(id, payload) VALUES(%s, %s)
                           ON DUPLICATE KEY UPDATE payload = VALUES(payload)""",
                        (1, payload),
                    )
                else:
                    cursor.execute(
                        """INSERT INTO bartender_app_state(id, payload) VALUES(%s, %s)
                           ON CONFLICT(id) DO UPDATE SET payload = EXCLUDED.payload""",
                        (1, payload),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def create_state_store(path: Path, backend: str = "internal", database_url: str = ""):
    normalized_backend = str(backend or "internal").strip().lower()
    if normalized_backend in ("internal", "sqlite"):
        return SQLiteStateStore(path)
    if normalized_backend in ("postgres", "postgresql"):
        return ExternalStateStore("postgresql", database_url)
    if normalized_backend in ("mariadb", "mysql"):
        return ExternalStateStore("mariadb", database_url)
    raise StorageConfigurationError(
        "STORAGE_BACKEND must be internal, postgresql, or mariadb."
    )
