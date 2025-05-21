# In backend/alembic/env.py

import os
from pathlib import Path # Add this if not present
from dotenv import load_dotenv # If you are using python-dotenv

# ... other imports ...
# from app.db import Base, SQLModel # Your models for target_metadata
# target_metadata = [Base.metadata, SQLModel.metadata]
# ...

def get_url():
    # Prioritize DATABASE_URL from the CI environment if "CI" env var is set (GitHub Actions sets CI=true)
    if os.getenv("CI"):
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            print(f"CI environment detected. Using DATABASE_URL from CI environment variables: {db_url[:db_url.find('@')]}@...") # Mask password
            return db_url
        else:
            raise ValueError("DATABASE_URL not found in CI environment variables!")
    else:
        # For local development, load from backend/.env
        # Path to backend/.env relative to this alembic/env.py file
        dotenv_path = Path(__file__).resolve().parent.parent / '.env'
        print(f"Local environment. Attempting to load .env from: {dotenv_path}")
        if dotenv_path.is_file():
            load_dotenv(dotenv_path=dotenv_path, override=True) # override=True ensures env vars can still override .env
        
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            print(f"Using DATABASE_URL from .env or environment: {db_url[:db_url.find('@')]}@...") # Mask password
            return db_url
        else:
            # Fallback for local if .env or DATABASE_URL is missing
            print("DATABASE_URL not found in .env or environment. Please set it.")
            # You might want to raise an error or have a default local URL here.
            # For safety, let's raise an error if not found locally either.
            raise ValueError("DATABASE_URL not found for local Alembic run. Check backend/.env or set environment variable.")


# In the run_migrations_offline() and run_migrations_online() functions:
# Replace how sqlalchemy.url is set:
# For example, in run_migrations_online():
#   configuration = config.get_section(config.config_ini_section)
#   configuration['sqlalchemy.url'] = get_url() # Use the new function
#   connectable = engine_from_config(
#       configuration,
#       prefix="sqlalchemy.",
#       poolclass=pool.NullPool,
#   )

# And in run_migrations_offline():
#   url = get_url() # Use the new function
#   context.configure(
#       url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
#   )