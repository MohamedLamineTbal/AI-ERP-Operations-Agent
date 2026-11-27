"""Integration tests for the PostgreSQL ERP database."""

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, selectinload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

if not os.getenv("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL is not configured; copy .env.example to .env to run "
        "the PostgreSQL integration test.",
        allow_module_level=True,
    )

from database import check_app_login, check_database_connection, engine
from models import Customer, Invoice, Order, OrderItem, Product, PurchaseOrder
from seed import create_admin_engine, grant_logins


ERP_TABLES = {
    "customers",
    "products",
    "orders",
    "order_items",
    "invoices",
    "purchase_orders",
}


def test_database_connection() -> None:
    assert check_database_connection() is True


def test_erp_tables_exist() -> None:
    assert ERP_TABLES.issubset(set(inspect(engine).get_table_names()))


def test_sample_records_exist() -> None:
    with Session(engine) as session:
        assert session.scalar(select(func.count(Customer.id))) >= 20
        assert session.scalar(select(func.count(Product.id))) >= 30
        assert session.scalar(select(func.count(Order.id))) >= 60
        assert session.scalar(select(func.count(OrderItem.id))) > 60
        assert session.scalar(select(func.count(Invoice.id))) >= 30
        assert session.scalar(select(func.count(PurchaseOrder.id))) > 0

        low_stock_count = session.scalar(
            select(func.count(Product.id)).where(
                Product.stock_quantity <= Product.reorder_threshold
            )
        )
        overdue_invoice_count = session.scalar(
            select(func.count(Invoice.id)).where(
                Invoice.status == "overdue", Invoice.due_date < date.today()
            )
        )
        assert low_stock_count > 0
        assert overdue_invoice_count > 0


def test_relationships_work() -> None:
    with Session(engine) as session:
        order = session.scalar(
            select(Order)
            .where(Order.status != "cancelled")
            .options(
                selectinload(Order.customer),
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.invoice),
            )
        )

        assert order is not None
        assert order.customer is not None
        assert order in order.customer.orders
        assert order.items
        assert all(item.order is order and item.product is not None for item in order.items)
        calculated_total = sum(
            (item.unit_price * item.quantity for item in order.items),
            start=Decimal("0.00"),
        )
        assert calculated_total == order.total_amount

        assert order.invoice is not None
        assert order.invoice.customer is order.customer
        assert order.invoice in order.customer.invoices

        purchase_order = session.scalar(
            select(PurchaseOrder).options(selectinload(PurchaseOrder.product))
        )
        assert purchase_order is not None
        assert purchase_order.product is not None
        assert purchase_order in purchase_order.product.purchase_orders


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE app_login_probe (id int)",
        "DROP TABLE order_items",
        "ALTER TABLE products ADD COLUMN probe int",
        "DELETE FROM orders",
        "UPDATE products SET price = 0",
        "UPDATE orders SET total_amount = 0",
        "INSERT INTO customers (name, company, email) VALUES ('x', 'x', 'x')",
        "SELECT rolname FROM pg_authid",
        "SELECT pg_read_file('/etc/hosts')",
    ],
)
def test_app_login_cannot_go_beyond_the_business_actions(statement: str) -> None:
    with engine.connect() as connection:
        with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
            connection.execute(text(statement))
        connection.rollback()


def test_the_app_refuses_to_run_as_an_admin_login() -> None:
    check_app_login()
    with pytest.raises(RuntimeError, match="admin rights"):
        check_app_login(create_admin_engine())


def test_seed_refuses_logins_that_share_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import seed

    monkeypatch.setattr(seed, "DATABASE_URL", seed.READONLY_DATABASE_URL)
    with create_admin_engine().connect() as connection:
        with pytest.raises(RuntimeError, match="different login"):
            grant_logins(connection)
