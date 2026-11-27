"""Integration tests for explicit ERP business actions."""

from datetime import datetime
from itertools import product as pairs
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import engine
from models import Invoice, Order, Product, PurchaseOrder
from seed import create_admin_engine
from tools.action_tools import (
    ORDER_TRANSITIONS,
    BusinessActionError,
    OrderStatus,
    cancel_order,
    create_purchase_order,
    mark_invoice_reminder_sent,
    update_order_status,
)

admin_engine = create_admin_engine()


MISSING_ID = 2_000_000_000


def test_create_purchase_order() -> None:
    with Session(engine) as session:
        product = session.scalar(select(Product).order_by(Product.id))
    assert product is not None
    product_id = product.id

    result = create_purchase_order(product_id, 25, "Replenish warehouse stock")
    try:
        with Session(engine) as session:
            purchase_order = session.get(PurchaseOrder, result["id"])
            assert purchase_order is not None
            assert purchase_order.product_id == product_id
            assert purchase_order.quantity == 25
            assert purchase_order.status == "draft"
            assert purchase_order.reason == "Replenish warehouse stock"
            assert purchase_order.unit_cost == product.unit_cost
    finally:
        with Session(admin_engine) as session, session.begin():
            purchase_order = session.get(PurchaseOrder, result["id"])
            if purchase_order is not None:
                session.delete(purchase_order)


def test_create_purchase_order_rejects_nonpositive_quantity() -> None:
    with pytest.raises(ValidationError):
        create_purchase_order(1, 0, "Invalid quantity")


def test_create_purchase_order_requires_existing_product() -> None:
    with pytest.raises(BusinessActionError, match="does not exist"):
        create_purchase_order(MISSING_ID, 10, "Product does not exist")


def test_cancel_order() -> None:
    with Session(engine) as session:
        order = session.scalar(select(Order).where(Order.status == "pending"))
        assert order is not None
        order_id = order.id
        original_status = order.status
        original_reason = order.cancellation_reason

    try:
        result = cancel_order(order_id, "Customer requested cancellation")
        assert result["status"] == "cancelled"

        with Session(engine) as session:
            cancelled_order = session.get(Order, order_id)
            assert cancelled_order is not None
            assert cancelled_order.status == "cancelled"
            assert (
                cancelled_order.cancellation_reason
                == "Customer requested cancellation"
            )
    finally:
        with Session(admin_engine) as session, session.begin():
            order = session.get(Order, order_id)
            assert order is not None
            order.status = original_status
            order.cancellation_reason = original_reason


def test_cancel_order_requires_existing_order() -> None:
    with pytest.raises(BusinessActionError, match="does not exist"):
        cancel_order(MISSING_ID, "Order does not exist")


def test_cancel_order_rejects_already_cancelled_order() -> None:
    with Session(engine) as session:
        order_id = session.scalar(
            select(Order.id).where(Order.status == "cancelled")
        )
    assert order_id is not None

    with pytest.raises(BusinessActionError, match="already cancelled"):
        cancel_order(order_id, "Duplicate cancellation")


@pytest.mark.parametrize("status", ["shipped", "completed"])
def test_cancel_order_rejects_fulfilled_order(status: str) -> None:
    with Session(engine) as session:
        order_id = session.scalar(select(Order.id).where(Order.status == status))
    assert order_id is not None

    with pytest.raises(BusinessActionError, match=status):
        cancel_order(order_id, "Too late to cancel")
    with Session(engine) as session:
        assert session.get(Order, order_id).status == status


def test_update_order_status() -> None:
    with Session(engine) as session:
        order = session.scalar(select(Order).where(Order.status == "pending"))
        assert order is not None
        order_id = order.id
        original_status = order.status

    try:
        result = update_order_status(order_id, "processing")
        assert result == {"id": order_id, "status": "processing"}

        with Session(engine) as session:
            updated_order = session.get(Order, order_id)
            assert updated_order is not None
            assert updated_order.status == "processing"
    finally:
        with Session(admin_engine) as session, session.begin():
            order = session.get(Order, order_id)
            assert order is not None
            order.status = original_status


def test_update_order_status_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        update_order_status(1, "not-a-real-status")  # type: ignore[arg-type]


def test_update_order_status_requires_existing_order() -> None:
    with pytest.raises(BusinessActionError, match="does not exist"):
        update_order_status(MISSING_ID, "processing")


def test_update_order_status_cannot_bypass_cancel_action() -> None:
    with pytest.raises(BusinessActionError, match="Use cancel_order"):
        update_order_status(1, "cancelled")


def test_mark_invoice_reminder_sent() -> None:
    with Session(engine) as session:
        invoice = session.scalar(select(Invoice).order_by(Invoice.id))
        assert invoice is not None
        invoice_id = invoice.id
        original_sent_at = invoice.reminder_sent_at

    try:
        result = mark_invoice_reminder_sent(invoice_id)
        assert result["id"] == invoice_id
        assert isinstance(result["reminder_sent_at"], datetime)

        with Session(engine) as session:
            updated_invoice = session.get(Invoice, invoice_id)
            assert updated_invoice is not None
            assert updated_invoice.reminder_sent_at is not None
    finally:
        with Session(admin_engine) as session, session.begin():
            invoice = session.get(Invoice, invoice_id)
            assert invoice is not None
            invoice.reminder_sent_at = original_sent_at


def test_mark_invoice_reminder_requires_existing_invoice() -> None:
    with pytest.raises(BusinessActionError, match="does not exist"):
        mark_invoice_reminder_sent(MISSING_ID)


FULFILMENT_STEPS = {
    ("pending", "processing"),
    ("processing", "shipped"),
    ("shipped", "completed"),
}


@pytest.mark.parametrize(
    "current, new",
    [p for p in pairs(get_args(OrderStatus), repeat=2) if p[1] != "cancelled"],
)
def test_order_status_only_moves_forward_one_step(current: str, new: str) -> None:
    with Session(engine) as session, session.begin():
        order = session.scalar(select(Order).order_by(Order.id))
        assert order is not None
        order.status = current
        session.flush()
        try:
            if (current, new) in FULFILMENT_STEPS:
                assert update_order_status(order.id, new, _session=session)["status"] == new
            else:
                with pytest.raises(BusinessActionError):
                    update_order_status(order.id, new, _session=session)
        finally:
            session.rollback()


def test_shipped_and_completed_orders_can_never_return_to_pending_or_processing() -> None:
    reachable = {"shipped", "completed"}
    frontier = list(reachable)
    while frontier:
        for status in ORDER_TRANSITIONS[frontier.pop()]:
            if status not in reachable:
                reachable.add(status)
                frontier.append(status)
    assert reachable.isdisjoint({"pending", "processing"})
