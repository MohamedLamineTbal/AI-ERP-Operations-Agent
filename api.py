"""HTTP API and web frontend for the ERP operations agent."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from config import BASE_DIR
from database import engine
from graph import (
    build_erp_graph,
    configure_outcome_log,
    create_thread_config,
    initial_state,
    resume_erp_graph,
)
from models import Customer, Invoice, Order, Product, PurchaseOrder


logger = logging.getLogger("erp_agent")

STEP_LABELS = {
    "understand_request": "Understanding the request",
    "generate_sql": "Looking up the data",
    "check_sql": "Looking up the data",
    "execute_sql": "Looking up the data",
    "answer_query": "Writing the answer",
    "verify_answer": "Checking the answer",
    "prepare_action": "Preparing the change",
    "validate_action": "Checking the change and its cost",
}
QUERY_FAILED = "I couldn't answer that."
ACTION_FAILED = "I couldn't prepare that change."


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class Decision(BaseModel):
    approved: bool


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str) + "\n"


def _final_answer(values: dict[str, Any]) -> dict[str, Any]:
    """Describe a finished run without exposing internal errors."""
    if values.get("error"):
        failed = QUERY_FAILED if values.get("request_type") == "query" else ACTION_FAILED
        return {"type": "answer", "answer": failed, "verified": False, "rows": None}
    rows = values.get("sql_result") if values.get("request_type") == "query" else None
    return {
        "type": "answer",
        "answer": values.get("answer"),
        "verified": values.get("verification") == "passed",
        "rows": rows,
    }


def _records(session: Session) -> dict[str, list[dict[str, Any]]]:
    orders = session.scalars(
        select(Order).options(selectinload(Order.items)).order_by(Order.id)
    )
    return {
        "customers": [
            {
                "id": c.id,
                "name": c.name,
                "company": c.company,
                "email": c.email,
                "createdAt": c.created_at.isoformat(),
            }
            for c in session.scalars(select(Customer).order_by(Customer.id))
        ],
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "sku": p.sku,
                "price": float(p.price),
                "cost": float(p.unit_cost),
                "stock": p.stock_quantity,
                "thr": p.reorder_threshold,
            }
            for p in session.scalars(select(Product).order_by(Product.id))
        ],
        "orders": [
            {
                "id": o.id,
                "customerId": o.customer_id,
                "status": o.status,
                "total": float(o.total_amount),
                "createdAt": o.created_at.isoformat(),
                "reason": o.cancellation_reason,
                "items": [
                    {
                        "productId": item.product_id,
                        "qty": item.quantity,
                        "unitPrice": float(item.unit_price),
                    }
                    for item in o.items
                ],
            }
            for o in orders
        ],
        "invoices": [
            {
                "id": i.id,
                "customerId": i.customer_id,
                "orderId": i.order_id,
                "amount": float(i.amount),
                "status": i.status,
                "dueDate": i.due_date.isoformat(),
                "reminder": i.reminder_sent_at.isoformat() if i.reminder_sent_at else None,
            }
            for i in session.scalars(select(Invoice).order_by(Invoice.id))
        ],
        "purchaseOrders": [
            {
                "id": po.id,
                "productId": po.product_id,
                "qty": po.quantity,
                "unitCost": float(po.unit_cost),
                "status": po.status,
                "reason": po.reason,
                "createdAt": po.created_at.isoformat(),
            }
            for po in session.scalars(select(PurchaseOrder).order_by(PurchaseOrder.id))
        ],
    }


def create_app(graph: Optional[Any] = None) -> FastAPI:
    """Build the API. The graph is created on first use unless one is injected."""
    app = FastAPI(title="ERP Operations Agent")
    compiled = {"graph": graph}
    pending: dict[str, dict[str, Any]] = {}

    def agent() -> Any:
        if compiled["graph"] is None:
            try:
                compiled["graph"] = build_erp_graph()
            except RuntimeError as error:
                logger.error("Agent unavailable: %s", error)
                raise HTTPException(503, "The assistant is not available right now.")
        return compiled["graph"]

    @app.get("/api/records")
    def records() -> dict[str, list[dict[str, Any]]]:
        with Session(engine) as session:
            return _records(session)

    @app.get("/api/approvals")
    def approvals() -> list[dict[str, Any]]:
        return list(pending.values())

    @app.post("/api/ask")
    def ask(request: AskRequest) -> StreamingResponse:
        try:
            start = initial_state(request.question)
        except ValueError as error:
            raise HTTPException(422, str(error))
        application = agent()
        config = create_thread_config()
        thread_id = config["configurable"]["thread_id"]

        def events() -> Iterator[str]:
            last_label = None
            approval = None
            try:
                for chunk in application.stream(start, config=config, stream_mode="updates"):
                    for node, update in chunk.items():
                        if node == "__interrupt__":
                            approval = update[0].value
                            continue
                        label = STEP_LABELS.get(node)
                        if label and label != last_label:
                            last_label = label
                            yield _line({"type": "step", "label": label})
                if approval is not None:
                    pending[thread_id] = {
                        "id": thread_id,
                        "question": request.question.strip(),
                        "approval": approval,
                    }
                    yield _line({"type": "approval", **pending[thread_id]})
                else:
                    yield _line(_final_answer(application.get_state(config).values))
            except Exception:
                logger.exception("Request failed: %s", request.question)
                yield _line({"type": "error", "message": "Something went wrong. Nothing was changed."})

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, decision: Decision) -> dict[str, Any]:
        if approval_id not in pending:
            raise HTTPException(404, "No change with that id is waiting for approval.")
        pending.pop(approval_id)
        values = resume_erp_graph(
            agent(), create_thread_config(approval_id), decision.approved
        )
        applied = values.get("approval_status") == "approved" and not values.get("error")
        if values.get("error"):
            logger.warning("Change not applied: %s", values["error"])
        return {
            "status": values.get("approval_status"),
            "applied": applied,
            "results": (values.get("action_results") or []) if applied else [],
        }

    app.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
    return app


configure_outcome_log()
app = create_app()
