"""Alembic environment for the storefront `web` schema.

We use a synchronous psycopg connection here (Alembic is sync-native) and we
restrict autogenerate / metadata operations to objects in our schema only.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.settings import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
target_metadata = Base.metadata


def _sync_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # Only manage objects inside our own schema.
    if hasattr(obj, "schema") and obj.schema and obj.schema != settings.db_schema:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(settings.database_url),
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=True,
        version_table_schema=settings.db_schema,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(settings.database_url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"')
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            include_schemas=True,
            version_table_schema=settings.db_schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
