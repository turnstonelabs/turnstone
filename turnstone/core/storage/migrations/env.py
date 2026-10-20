"""Alembic environment for turnstone storage migrations."""

from alembic import context

from turnstone.core.storage._schema import metadata

target_metadata = metadata


def run_migrations_online() -> None:
    """Run migrations using the engine from alembic config."""
    from sqlalchemy import engine_from_config, pool

    config = context.config
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Error text names the statement, not its values; they stay on exc.params.
        hide_parameters=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
