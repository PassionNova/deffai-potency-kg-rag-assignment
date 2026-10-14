from alembic import context
from sqlalchemy import create_engine, pool

from potency.settings import get_settings


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().owner_database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
