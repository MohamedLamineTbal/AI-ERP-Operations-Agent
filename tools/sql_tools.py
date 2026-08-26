"""Read-only SQL tools intended for the future AI query layer."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from database import engine


class ReadOnlySQLValidationError(ValueError):
    """Raised when a query does not meet the read-only SQL policy."""


class ReadOnlySQLExecutionError(RuntimeError):
    """Raised when PostgreSQL cannot execute a validated read-only query."""


FORBIDDEN_KEYWORDS = {
    "alter",
    "call",
    "comment",
    "copy",
    "create",
    "delete",
    "do",
    "drop",
    "grant",
    "insert",
    "into",
    "merge",
    "refresh",
    "reindex",
    "revoke",
    "truncate",
    "update",
    "vacuum",
}

FORBIDDEN_FUNCTIONS = {
    "lo_export",
    "lo_import",
    "nextval",
    "pg_cancel_backend",
    "pg_create_restore_point",
    "pg_logical_emit_message",
    "pg_notify",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_switch_wal",
    "pg_terminate_backend",
    "set_config",
    "setval",
}
FORBIDDEN_FUNCTION_PREFIXES = ("dblink_", "pg_advisory_")

_DOLLAR_QUOTE_START = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _tokenize_sql(query: str) -> list[str]:
    """Tokenize enough PostgreSQL syntax to enforce the read-only policy.

    Comments and quoted values are ignored so words such as ``delete`` inside
    a string or comment are not mistaken for executable SQL.
    """
    tokens: list[str] = []
    index = 0

    while index < len(query):
        character = query[index]

        if character.isspace():
            index += 1
            continue

        if query.startswith("--", index):
            newline = query.find("\n", index + 2)
            index = len(query) if newline == -1 else newline + 1
            continue

        if query.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(query) and depth:
                if query.startswith("/*", index):
                    depth += 1
                    index += 2
                elif query.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ReadOnlySQLValidationError("SQL contains an unclosed comment.")
            continue

        if character == "'":
            index += 1
            while index < len(query):
                if query[index] == "'":
                    if index + 1 < len(query) and query[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ReadOnlySQLValidationError(
                    "SQL contains an unclosed string literal."
                )
            tokens.append("<value>")
            continue

        if character == '"':
            index += 1
            identifier: list[str] = []
            while index < len(query):
                if query[index] == '"':
                    if index + 1 < len(query) and query[index + 1] == '"':
                        identifier.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                identifier.append(query[index])
                index += 1
            else:
                raise ReadOnlySQLValidationError(
                    "SQL contains an unclosed quoted identifier."
                )
            tokens.append(f"<quoted_identifier:{''.join(identifier).lower()}>")
            continue

        dollar_quote = _DOLLAR_QUOTE_START.match(query, index)
        if dollar_quote:
            delimiter = dollar_quote.group(0)
            closing_index = query.find(delimiter, dollar_quote.end())
            if closing_index == -1:
                raise ReadOnlySQLValidationError(
                    "SQL contains an unclosed dollar-quoted string."
                )
            index = closing_index + len(delimiter)
            tokens.append("<value>")
            continue

        word = _WORD.match(query, index)
        if word:
            tokens.append(word.group(0).lower())
            index = word.end()
            continue

        tokens.append(character)
        index += 1

    return tokens


def validate_readonly_sql(query: str) -> bool:
    """Validate that *query* is one SELECT statement with no write keywords.

    Returns ``True`` for a valid query and raises
    :class:`ReadOnlySQLValidationError` otherwise.
    """
    if not isinstance(query, str) or not query.strip():
        raise ReadOnlySQLValidationError("SQL query must be a non-empty string.")

    if "\x00" in query:
        raise ReadOnlySQLValidationError("SQL query contains an invalid null byte.")

    tokens = _tokenize_sql(query)
    if not tokens:
        raise ReadOnlySQLValidationError("SQL query must contain a statement.")

    semicolons = [index for index, token in enumerate(tokens) if token == ";"]
    if len(semicolons) > 1 or (semicolons and semicolons[0] != len(tokens) - 1):
        raise ReadOnlySQLValidationError("Only one SQL statement is allowed.")

    statement_tokens = tokens[:-1] if tokens[-1] == ";" else tokens
    words = [token for token in statement_tokens if _WORD.fullmatch(token)]
    if not words or words[0] != "select":
        raise ReadOnlySQLValidationError("Only SELECT queries are allowed.")

    forbidden = sorted(FORBIDDEN_KEYWORDS.intersection(words))
    if forbidden:
        raise ReadOnlySQLValidationError(
            f"Read-only SQL cannot contain the keyword: {forbidden[0].upper()}."
        )

    called_functions = set()
    for index, token in enumerate(statement_tokens[:-1]):
        if statement_tokens[index + 1] != "(":
            continue
        if _WORD.fullmatch(token):
            called_functions.add(token)
        elif token.startswith("<quoted_identifier:"):
            called_functions.add(token[len("<quoted_identifier:") : -1])

    forbidden_functions = sorted(
        function
        for function in called_functions
        if function in FORBIDDEN_FUNCTIONS
        or function.startswith(FORBIDDEN_FUNCTION_PREFIXES)
    )
    if forbidden_functions:
        raise ReadOnlySQLValidationError(
            "Read-only SQL cannot call the stateful or unsafe function: "
            f"{forbidden_functions[0]}."
        )

    return True


def list_tables() -> list[str]:
    """Return the names of tables in PostgreSQL's public schema."""
    return sorted(inspect(engine).get_table_names(schema="public"))


def get_table_schema(table_name: str) -> dict[str, Any]:
    """Return columns and constraints for a public PostgreSQL table."""
    if not isinstance(table_name, str) or table_name not in list_tables():
        raise ValueError(f"Unknown public table: {table_name!r}")

    inspector = inspect(engine)
    primary_key = inspector.get_pk_constraint(table_name, schema="public")

    return {
        "table_name": table_name,
        "columns": [
            {
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": column["nullable"],
                "default": column.get("default"),
                "primary_key": column["name"]
                in primary_key.get("constrained_columns", []),
            }
            for column in inspector.get_columns(table_name, schema="public")
        ],
        "primary_key": primary_key.get("constrained_columns", []),
        "foreign_keys": inspector.get_foreign_keys(table_name, schema="public"),
        "unique_constraints": inspector.get_unique_constraints(
            table_name, schema="public"
        ),
        "check_constraints": inspector.get_check_constraints(
            table_name, schema="public"
        ),
    }


def execute_readonly_sql(query: str) -> list[dict[str, Any]]:
    """Execute a validated SELECT in a PostgreSQL read-only transaction."""
    validate_readonly_sql(query)

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                result = connection.execute(text(query))
                rows = [dict(row._mapping) for row in result]
            finally:
                transaction.rollback()
    except SQLAlchemyError as error:
        details = str(getattr(error, "orig", error)).strip()
        raise ReadOnlySQLExecutionError(
            f"PostgreSQL could not execute the read-only query: {details}"
        ) from error

    return rows
