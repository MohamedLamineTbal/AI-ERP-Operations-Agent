"""SQLAlchemy engines and PostgreSQL login checks.

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


def check_app_login(app_engine: Engine = engine) -> None:
    """Refuse a login with admin rights or ownership of the ERP tables.

    Owners can drop or alter their tables, so the app must not be one.
    Raises RuntimeError naming what is wrong.
    """
    with app_engine.connect() as connection:
        privileged, owns_tables = connection.execute(
            text(
                "SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb "
                "OR r.rolreplication OR r.rolbypassrls, "
                "EXISTS (SELECT 1 FROM pg_tables t WHERE t.schemaname = 'public' "
                "AND pg_has_role(current_user, t.tableowner, 'MEMBER')) "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        ).one()
    if privileged or owns_tables:
        raise RuntimeError(
            "DATABASE_URL uses a login with admin rights or that owns the ERP "
            "tables. Point it at the app login seed.py creates, and put the "
            "admin login in DATABASE_ADMIN_URL."
        )
