# Path: backend/alembic/env.py
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# --- Add this section to load .env file ---
from dotenv import load_dotenv
# Construct the path to the .env file located in the parent directory (backend/)
# This assumes env.py is in alembic/ and .env is in backend/
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(dotenv_path):
    print(f"Loading .env from: {os.path.abspath(dotenv_path)}")
    load_dotenv(dotenv_path=dotenv_path, override=True) # override=True ensures .env takes precedence
else:
    print(f"Warning: .env file not found at {os.path.abspath(dotenv_path)}. "
          "Ensure DATABASE_URL is set in your shell environment for local Alembic runs against RDS.")
# --- End of section to load .env file ---

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# --- Get DATABASE_URL directly from environment ---
# This should be populated by load_dotenv() from backend/.env or by the shell.
actual_database_url = os.environ.get("DATABASE_URL")

if not actual_database_url:
    # If still not found, as a last resort, try to get it from alembic.ini
    # This might happen if .env loading failed and it's not in the shell environment.
    # However, if alembic.ini itself uses %(DATABASE_URL)s, this will also fail.
    print("DATABASE_URL not found in environment after attempting to load .env.")
    print("Attempting to read 'sqlalchemy.url' directly from alembic.ini as a fallback.")
    actual_database_url = config.get_main_option("sqlalchemy.url")
    if not actual_database_url or "%(DATABASE_URL)s" in actual_database_url: # Check if it's still unresolved
        raise ValueError(
            "DATABASE_URL is not set in the environment (e.g., via backend/.env or shell) "
            "and alembic.ini's sqlalchemy.url either depends on it or is missing. "
            "Please ensure backend/.env contains the correct DATABASE_URL for RDS."
        )
else:
    # Sanitize for printing - hide password part
    url_parts = actual_database_url.split('@')
    if len(url_parts) > 1:
        auth_part = url_parts[0].split(':')
        if len(auth_part) > 1:
            sanitized_url_start = f"{auth_part[0]}:********@"
            sanitized_url = sanitized_url_start + url_parts[1]
            print(f"Using DATABASE_URL from environment: {sanitized_url}")
        else:
            print(f"Using DATABASE_URL from environment (could not sanitize for printing): {actual_database_url}")
    else:
        print(f"Using DATABASE_URL from environment: {actual_database_url}")


# IMPORTANT: Override the sqlalchemy.url in the config object that Alembic uses.
# This ensures that subsequent operations within Alembic use the resolved URL.
config.set_main_option("sqlalchemy.url", actual_database_url)


# add your model's MetaData object here
# for 'autogenerate' support
from app.db import Base # Import Base from your application's model definition
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    # The 'sqlalchemy.url' in the config object should now be the resolved RDS URL.
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # engine_from_config will use the 'sqlalchemy.url' we just set in the config object.
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()