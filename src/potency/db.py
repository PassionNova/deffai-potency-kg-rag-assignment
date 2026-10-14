"""Database access. The only way to obtain a connection for evidence data is `scoped_session`, which sets
the row-level-security scope for the transaction. The application role cannot bypass RLS."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from potency.settings import get_settings
from potency.tenancy import Scope

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row, "autocommit": False, "keepalives": 1, "keepalives_idle": 30},
            configure=_configure,
            check=ConnectionPool.check_connection,
            max_idle=120,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def scoped_session(scope: Scope) -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true), set_config('app.project_id', %s, true)",
                (scope.tenant_id, scope.project_id),
            )
            yield conn


@contextmanager
def unscoped_spend_session() -> Iterator[psycopg.Connection]:
    """Only for the global numeric spend counter (`model_spend`), which holds no scope content."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn
