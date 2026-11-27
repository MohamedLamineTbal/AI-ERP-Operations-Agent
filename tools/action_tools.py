"""Explicit, validated business actions for the ERP application.

These functions use ORM operations and are never exposed to the model. The
graph calls them only after a human approves the matching proposal.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import engine
from models import Invoice, Order, Product, PurchaseOrder


OrderStatus = Literal["pending", "processing", "shipped", "completed", "cancelled"]

# Fulfilment only moves forward. Reaching "cancelled" goes through cancel_order,
# which records a reason.
ORDER_TRANSITIONS: dict[str, tuple[OrderStatus, ...]] = {
    "pending": ("processing", "cancelled"),
    "processing": ("shipped", "cancelled"),
    "shipped": ("completed",),
    "completed": (),
    "cancelled": (),
}


def can_cancel(status: str) -> bool:
    return "cancelled" in ORDER_TRANSITIONS[status]


def next_order_statuses(status: str) -> list[OrderStatus]:
    """The statuses update_order_status accepts from *status*."""
    return [s for s in ORDER_TRANSITIONS[status] if s != "cancelled"]


class BusinessActionError(ValueError):
    """Raised when an action violates an ERP business rule."""


class ActionInput(BaseModel):
    """Shared strict validation behavior for business-action inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreatePurchaseOrderInput(ActionInput):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class CancelOrderInput(ActionInput):
    order_id: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class UpdateOrderStatusInput(ActionInput):
    order_id: int = Field(gt=0)
    new_status: OrderStatus


class MarkInvoiceReminderSentInput(ActionInput):
    invoice_id: int = Field(gt=0)


def get_unit_costs(product_ids: Iterable[int]) -> dict[int, Decimal]:
    """Return the supplier unit cost of each listed product that exists."""
    with Session(engine) as session:
        rows = session.execute(
            select(Product.id, Product.unit_cost).where(
                Product.id.in_(set(product_ids))
            )
        )
        return {product_id: unit_cost for product_id, unit_cost in rows}


def create_purchase_order(
    product_id: int,
    quantity: int,
    reason: str,
    *,
    _session: Session | None = None,
) -> dict[str, Any]:
    """Create a draft purchase order for an existing product."""
    action = CreatePurchaseOrderInput(
        product_id=product_id, quantity=quantity, reason=reason
    )

    if _session is not None:
        return _create_purchase_order(_session, action)
    with Session(engine) as session, session.begin():
        return _create_purchase_order(session, action)


def _create_purchase_order(
    session: Session, action: CreatePurchaseOrderInput
) -> dict[str, Any]:
    """Create a purchase order within a caller-managed transaction."""
    # No row lock: locking needs UPDATE rights on products, which the app login
    # doesn't have, and the foreign key already keeps the product until commit.
    product = session.get(Product, action.product_id)
    if product is None:
        raise BusinessActionError(f"Product {action.product_id} does not exist.")

    purchase_order = PurchaseOrder(
        product=product,
        quantity=action.quantity,
        unit_cost=product.unit_cost,
        status="draft",
        reason=action.reason,
    )
    session.add(purchase_order)
    session.flush()

    return {
        "id": purchase_order.id,
        "product_id": purchase_order.product_id,
        "quantity": purchase_order.quantity,
        "unit_cost": purchase_order.unit_cost,
        "status": purchase_order.status,
        "reason": purchase_order.reason,
    }


def cancel_order(
    order_id: int, reason: str, *, _session: Session | None = None
) -> dict[str, Any]:
    """Cancel an eligible order and retain the cancellation reason."""
    action = CancelOrderInput(order_id=order_id, reason=reason)

    if _session is not None:
        return _cancel_order(_session, action)
    with Session(engine) as session, session.begin():
        return _cancel_order(session, action)


def _cancel_order(session: Session, action: CancelOrderInput) -> dict[str, Any]:
    """Cancel an order within a caller-managed transaction."""
    order = session.scalar(
        select(Order).where(Order.id == action.order_id).with_for_update()
    )
    if order is None:
        raise BusinessActionError(f"Order {action.order_id} does not exist.")
    if order.status == "cancelled":
        raise BusinessActionError(f"Order {action.order_id} is already cancelled.")
    if not can_cancel(order.status):
        raise BusinessActionError(
            f"Order {action.order_id} is {order.status} and cannot be cancelled."
        )

    order.status = "cancelled"
    order.cancellation_reason = action.reason
    session.flush()

    return {
        "id": order.id,
        "status": order.status,
        "cancellation_reason": order.cancellation_reason,
    }


def update_order_status(
    order_id: int,
    new_status: OrderStatus,
    *,
    _session: Session | None = None,
) -> dict[str, Any]:
    """Set an order to a validated status without bypassing cancellation rules."""
    action = UpdateOrderStatusInput(order_id=order_id, new_status=new_status)

    if _session is not None:
        return _update_order_status(_session, action)
    with Session(engine) as session, session.begin():
        return _update_order_status(session, action)


def _update_order_status(
    session: Session, action: UpdateOrderStatusInput
) -> dict[str, Any]:
    """Update an order within a caller-managed transaction."""
    order = session.scalar(
        select(Order).where(Order.id == action.order_id).with_for_update()
    )
    if order is None:
        raise BusinessActionError(f"Order {action.order_id} does not exist.")
    if action.new_status == "cancelled":
        raise BusinessActionError(
            "Use cancel_order to enforce cancellation rules and record a reason."
        )
    if order.status == "cancelled":
        raise BusinessActionError(
            f"Cancelled order {action.order_id} cannot change status."
        )
    if action.new_status == order.status:
        raise BusinessActionError(
            f"Order {action.order_id} is already {order.status}."
        )
    allowed = next_order_statuses(order.status)
    if action.new_status not in allowed:
        only = f" It can only move to {' or '.join(allowed)}." if allowed else ""
        raise BusinessActionError(
            f"Order {action.order_id} is {order.status} and can't move to "
            f"{action.new_status}.{only}"
        )

    order.status = action.new_status
    session.flush()

    return {"id": order.id, "status": order.status}


def mark_invoice_reminder_sent(
    invoice_id: int, *, _session: Session | None = None
) -> dict[str, Any]:
    """Record the time at which an existing invoice reminder was sent."""
    action = MarkInvoiceReminderSentInput(invoice_id=invoice_id)

    if _session is not None:
        return _mark_invoice_reminder_sent(_session, action)
    with Session(engine) as session, session.begin():
        return _mark_invoice_reminder_sent(session, action)


def _mark_invoice_reminder_sent(
    session: Session, action: MarkInvoiceReminderSentInput
) -> dict[str, Any]:
    """Mark a reminder within a caller-managed transaction."""
    invoice = session.scalar(
        select(Invoice).where(Invoice.id == action.invoice_id).with_for_update()
    )
    if invoice is None:
        raise BusinessActionError(f"Invoice {action.invoice_id} does not exist.")

    invoice.reminder_sent_at = datetime.now(timezone.utc)
    session.flush()

    return {
        "id": invoice.id,
        "reminder_sent_at": invoice.reminder_sent_at,
    }
