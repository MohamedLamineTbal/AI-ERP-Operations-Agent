"""SQLAlchemy engines and a minimal PostgreSQL connectivity check.

``engine`` runs the application's own queries and approved actions.
``readonly_engine`` runs model-generated SQL as a login that can only read
the ERP tables.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config import DATABASE_URL, READONLY_DATABASE_URL


engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True)
readonly_engine: Engine = create_engine(READONLY_DATABASE_URL, pool_pre_ping=True)


def check_database_connection() -> bool:
    """Return True when PostgreSQL successfully executes a simple query."""
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        return result.scalar_one() == 1
