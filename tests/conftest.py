"""Run every test against a disposable test database, never the development one.

This module runs before any test imports ``config``, so it repoints both database
URLs at TEST_DATABASE_URL first. The test database is rebuilt from the seed at the
start of each run, and reseeded after any test that committed a write.
"""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

_settings = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
if not _settings.get("TEST_DATABASE_URL"):
    raise pytest.UsageError(
        "TEST_DATABASE_URL is not set. Add a separate test database URL to .env."
    )
_test_url = make_url(_settings["TEST_DATABASE_URL"])
_dev_url = make_url(_settings["DATABASE_URL"])
if (_test_url.host, _test_url.port, _test_url.database) == (
    _dev_url.host,
    _dev_url.port,
    _dev_url.database,
):
    raise pytest.UsageError(
        "TEST_DATABASE_URL points at the development database. Use a separate one."
    )

os.environ["DATABASE_URL"] = _test_url.render_as_string(hide_password=False)
os.environ["READONLY_DATABASE_URL"] = (
    make_url(_settings["READONLY_DATABASE_URL"])
    .set(database=_test_url.database)
    .render_as_string(hide_password=False)
)

from database import engine  # noqa: E402  (must follow the URL override above)
from models import Base  # noqa: E402
from seed import seed_database  # noqa: E402

_wrote = {"flag": False}


@event.listens_for(engine, "commit")
def _remember_write(connection):
    _wrote["flag"] = True


def _create_test_database_if_missing() -> None:
    server = create_engine(
        _test_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with server.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": _test_url.database},
        )
        if not exists:
            quoted = connection.scalar(
                text("SELECT format('CREATE DATABASE %I', CAST(:name AS text))"),
                {"name": _test_url.database},
            )
            connection.exec_driver_sql(quoted)
    server.dispose()


def _reseed() -> None:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    seed_database()
    _wrote["flag"] = False


@pytest.fixture(scope="session", autouse=True)
def test_database():
    _create_test_database_if_missing()
    Base.metadata.drop_all(engine)
    seed_database()
    _wrote["flag"] = False
    yield


@pytest.fixture(autouse=True)
def fresh_data_after_writes(test_database):
    yield
    if _wrote["flag"]:
        _reseed()
