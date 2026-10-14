"""Offline test harness: model test doubles + a dedicated `potency_test` database (created and migrated per session).

Nothing here calls a paid model or touches the main `potency` database contents."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
HOST = os.environ.get("POTENCY_TEST_PG", "localhost:55433")
TEST_DB = "potency_test"

TEST_ENV = {
    "POTENCY_EMBEDDER": "fake",
    "POTENCY_LLM": "fake",
    "DATABASE_URL": f"postgresql://potency_app:potency_app_dev@{HOST}/{TEST_DB}",
    "OWNER_DATABASE_URL": f"postgresql://potency_owner:potency_owner_dev@{HOST}/{TEST_DB}",
    "POTENCY_MAX_USD": "0",
}
os.environ.update(TEST_ENV)


@pytest.fixture(scope="session", autouse=True)
def test_database():
    admin = f"postgresql://potency_owner:potency_owner_dev@{HOST}/potency"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO, check=True,
                   env={**os.environ, **TEST_ENV})
    from potency.settings import reset_settings

    reset_settings()
    yield
    from potency.db import close_pool

    close_pool()


@pytest.fixture
def subprocess_env() -> dict:
    return {**os.environ, **TEST_ENV}
