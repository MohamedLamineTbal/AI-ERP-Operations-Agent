"""Integration tests for the read-only SQL tools."""

import pytest

from tools.sql_tools import (
    ReadOnlySQLExecutionError,
    ReadOnlySQLValidationError,
    execute_readonly_sql,
    get_table_schema,
    list_tables,
    validate_readonly_sql,
)


def test_list_tables_and_get_schema() -> None:
    tables = list_tables()
    assert "customers" in tables
    assert "orders" in tables

    schema = get_table_schema("order_items")
    assert schema["primary_key"] == ["id"]
    assert {column["name"] for column in schema["columns"]} == {
        "id",
        "order_id",
        "product_id",
        "quantity",
        "unit_price",
    }
    assert len(schema["foreign_keys"]) == 2


def test_valid_select_query() -> None:
    assert validate_readonly_sql("SELECT id, name FROM customers LIMIT 3") is True

    rows = execute_readonly_sql("SELECT id, name FROM customers ORDER BY id LIMIT 3").rows
    assert len(rows) == 3
    assert set(rows[0]) == {"id", "name"}


def test_filtering_query() -> None:
    rows = execute_readonly_sql(
        """
        SELECT id, stock_quantity, reorder_threshold
        FROM products
        WHERE stock_quantity < reorder_threshold
        ORDER BY id
        """
    ).rows
    assert rows
    assert all(
        row["stock_quantity"] < row["reorder_threshold"] for row in rows
    )


def test_join_query() -> None:
    rows = execute_readonly_sql(
        """
        SELECT o.id AS order_id, c.name AS customer_name
        FROM orders AS o
        JOIN customers AS c ON c.id = o.customer_id
        ORDER BY o.id
        LIMIT 5
        """
    ).rows
    assert len(rows) == 5
    assert all(row["order_id"] and row["customer_name"] for row in rows)


def test_multi_table_query() -> None:
    rows = execute_readonly_sql(
        """
        SELECT c.name AS customer_name,
               o.id AS order_id,
               p.sku,
               oi.quantity
        FROM customers AS c
        JOIN orders AS o ON o.customer_id = c.id
        JOIN order_items AS oi ON oi.order_id = o.id
        JOIN products AS p ON p.id = oi.product_id
        ORDER BY o.id, oi.id
        LIMIT 10
        """
    ).rows
    assert len(rows) == 10
    assert set(rows[0]) == {"customer_name", "order_id", "sku", "quantity"}
    assert all(row["quantity"] > 0 for row in rows)


def test_aggregation_query() -> None:
    rows = execute_readonly_sql(
        """
        SELECT status, COUNT(*) AS order_count, SUM(total_amount) AS revenue
        FROM orders
        GROUP BY status
        ORDER BY status
        """
    ).rows
    assert rows
    assert sum(row["order_count"] for row in rows) == 60
    assert all(row["revenue"] >= 0 for row in rows)


@pytest.mark.parametrize(
    "query, keyword",
    [
        ("DELETE FROM customers WHERE id = 1", "SELECT"),
        ("UPDATE products SET stock_quantity = 0", "SELECT"),
        ("DROP TABLE orders", "SELECT"),
        ("SELECT 1; DELETE FROM customers", "statement"),
        ("SELECT * INTO copied_customers FROM customers", "INTO"),
    ],
)
def test_state_changing_sql_is_rejected(query: str, keyword: str) -> None:
    with pytest.raises(ReadOnlySQLValidationError, match=keyword):
        execute_readonly_sql(query)


def test_keywords_inside_values_and_comments_are_allowed() -> None:
    rows = execute_readonly_sql(
        "SELECT 'DELETE is text' AS message /* DROP is a comment */"
    ).rows
    assert rows == [{"message": "DELETE is text"}]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_advisory_lock(12345)",
        'SELECT "pg_advisory_lock"(12345)',
        "SELECT pg_notify('erp_events', 'changed')",
        "SELECT nextval('customers_id_seq')",
    ],
)
def test_stateful_select_functions_are_rejected(query: str) -> None:
    with pytest.raises(ReadOnlySQLValidationError, match="function"):
        validate_readonly_sql(query)


def test_malformed_sql_is_reported() -> None:
    with pytest.raises(ReadOnlySQLExecutionError):
        execute_readonly_sql("SELECT id, FROM customers")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_read_file('/etc/hosts')",
        "SELECT * FROM pg_ls_dir('.')",
        "SELECT rolname FROM pg_authid",
        "SELECT query_to_xml('SELECT pg_read_file(''/etc/hosts'')', true, false, '')",
    ],
)
def test_readonly_sql_cannot_reach_beyond_the_erp_tables(query: str) -> None:
    with pytest.raises(ReadOnlySQLExecutionError, match="permission denied"):
        execute_readonly_sql(query)


@pytest.mark.parametrize("matching, truncated", [(10, False), (11, True), (50, True)])
def test_results_stop_at_the_row_cap(matching: int, truncated: bool) -> None:
    result = execute_readonly_sql(
        f"SELECT g FROM generate_series(1, {matching}) g ORDER BY g", max_rows=10
    )

    assert result.rows == [{"g": i} for i in range(1, min(matching, 10) + 1)]
    assert result.truncated is truncated
