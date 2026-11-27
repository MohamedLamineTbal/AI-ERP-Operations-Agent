"""End-to-end tests for query-then-action LangGraph requests."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import engine
from graph import (
    AgentState,
    ERPGraphNodes,
    build_erp_graph,
    create_thread_config,
    get_interrupt_payload,
    resume_erp_graph,
    run_erp_graph,
)
from models import Invoice, Product, PurchaseOrder
from seed import create_admin_engine
from tools.sql_tools import execute_readonly_sql, validate_readonly_sql

admin_engine = create_admin_engine()


class QueryThenActionNodes:
    """Use deterministic SQL while exercising production batch-action nodes."""

    def __init__(self, action_tool: str, query: str) -> None:
        self.action_tool = action_tool
        self.query = query

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        return {
            "request_type": "query_action",
            "action_tool": self.action_tool,
            "error": None,
        }

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        return {"generated_sql": self.query, "error": None}

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        validate_readonly_sql(state["generated_sql"])
        return {"generated_sql": state["generated_sql"], "error": None}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        result = execute_readonly_sql(state["generated_sql"])
        return {"sql_result": result.rows, "sql_truncated": result.truncated, "error": None}

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        answer = state.get("error") or "query complete"
        return {"answer": answer, "messages": [AIMessage(content=answer)]}

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Query-then-action requests end in approval, not an answer")

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.prepare_action(self, state)

    def _prepare_query_actions(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes._prepare_query_actions(self, state)

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.validate_action(self, state)

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.request_approval(self, state)

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.execute_action(self, state)

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.finish_action(self, state)


def test_restock_matching_products_after_batch_approval() -> None:
    with Session(engine) as session:
        products = list(session.scalars(select(Product).order_by(Product.id).limit(2)))
        assert len(products) == 2
        product_ids = [product.id for product in products]
        original_inventory = {
            product.id: (product.stock_quantity, product.reorder_threshold)
            for product in products
        }

    try:
        with Session(admin_engine) as session, session.begin():
            first = session.get(Product, product_ids[0])
            second = session.get(Product, product_ids[1])
            assert first is not None and second is not None
            first.stock_quantity, first.reorder_threshold = 2, 10
            second.stock_quantity, second.reorder_threshold = 3, 8

        query = f"""
            SELECT id AS product_id, name, stock_quantity, reorder_threshold
            FROM products
            WHERE id IN ({product_ids[0]}, {product_ids[1]})
              AND stock_quantity < reorder_threshold
            ORDER BY id
        """
        nodes = QueryThenActionNodes("create_purchase_order", query)
        graph = build_erp_graph(nodes)
        config = create_thread_config(f"restock-approved-{uuid4()}")

        interrupted = run_erp_graph(
            "Restock every product below its reorder threshold.", graph, config
        )
        approval = get_interrupt_payload(interrupted)

        assert approval is not None, interrupted
        assert approval["type"] == "action_approval"
        assert [action["arguments"] for action in approval["actions"]] == [
            {"product_id": product_ids[0], "quantity": 18},
            {"product_id": product_ids[1], "quantity": 13},
        ]
        with Session(engine) as session:
            costs = [session.get(Product, pid).unit_cost for pid in product_ids]
        assert [action["line_cost"] for action in approval["actions"]] == [
            str(costs[0] * 18),
            str(costs[1] * 13),
        ]
        assert approval["total_cost"] == str(costs[0] * 18 + costs[1] * 13)

        with Session(engine) as session:
            assert not list(
                session.scalars(
                    select(PurchaseOrder).where(
                        PurchaseOrder.reason == "Automatic low-stock replenishment"
                    )
                )
            )

        completed = resume_erp_graph(graph, config, approved=True)
        created_ids = [result["id"] for result in completed["action_results"]]
        try:
            with Session(engine) as session:
                created = list(
                    session.scalars(
                        select(PurchaseOrder)
                        .where(PurchaseOrder.id.in_(created_ids))
                        .order_by(PurchaseOrder.product_id)
                    )
                )
                assert [(row.product_id, row.quantity) for row in created] == [
                    (product_ids[0], 18),
                    (product_ids[1], 13),
                ]
            assert "2 actions approved and executed" in completed["answer"]
        finally:
            with Session(admin_engine) as session, session.begin():
                for purchase_order in session.scalars(
                    select(PurchaseOrder).where(PurchaseOrder.id.in_(created_ids))
                ):
                    session.delete(purchase_order)
    finally:
        with Session(admin_engine) as session, session.begin():
            for product_id, values in original_inventory.items():
                product = session.get(Product, product_id)
                assert product is not None
                product.stock_quantity, product.reorder_threshold = values


def test_send_reminders_for_matching_overdue_invoices_after_approval() -> None:
    with Session(engine) as session:
        invoices = list(session.scalars(select(Invoice).order_by(Invoice.id).limit(2)))
        assert len(invoices) == 2
        invoice_ids = [invoice.id for invoice in invoices]
        original_values = {
            invoice.id: (
                invoice.amount,
                invoice.status,
                invoice.due_date,
                invoice.reminder_sent_at,
            )
            for invoice in invoices
        }

    try:
        with Session(admin_engine) as session, session.begin():
            first = session.get(Invoice, invoice_ids[0])
            second = session.get(Invoice, invoice_ids[1])
            assert first is not None and second is not None
            for invoice, amount in ((first, "6001.00"), (second, "8250.00")):
                invoice.amount = Decimal(amount)
                invoice.status = "overdue"
                invoice.due_date = date.today() - timedelta(days=10)
                invoice.reminder_sent_at = None

        query = f"""
            SELECT id AS invoice_id, amount, due_date, status, reminder_sent_at
            FROM invoices
            WHERE id IN ({invoice_ids[0]}, {invoice_ids[1]})
              AND status = 'overdue'
              AND due_date < CURRENT_DATE
              AND amount > 5000
              AND reminder_sent_at IS NULL
            ORDER BY id
        """
        nodes = QueryThenActionNodes("mark_invoice_reminder_sent", query)
        graph = build_erp_graph(nodes)
        config = create_thread_config(f"reminders-approved-{uuid4()}")

        interrupted = run_erp_graph(
            "Send reminders for every overdue invoice above $5,000.", graph, config
        )
        approval = get_interrupt_payload(interrupted)

        assert approval is not None, interrupted
        assert [action["arguments"] for action in approval["actions"]] == [
            {"invoice_id": invoice_ids[0]},
            {"invoice_id": invoice_ids[1]},
        ]
        with Session(engine) as session:
            assert all(
                session.get(Invoice, invoice_id).reminder_sent_at is None
                for invoice_id in invoice_ids
            )

        completed = resume_erp_graph(graph, config, approved=True)

        with Session(engine) as session:
            assert all(
                session.get(Invoice, invoice_id).reminder_sent_at is not None
                for invoice_id in invoice_ids
            )
        assert len(completed["action_results"]) == 2
        assert "2 actions approved and executed" in completed["answer"]
    finally:
        with Session(admin_engine) as session, session.begin():
            for invoice_id, values in original_values.items():
                invoice = session.get(Invoice, invoice_id)
                assert invoice is not None
                (
                    invoice.amount,
                    invoice.status,
                    invoice.due_date,
                    invoice.reminder_sent_at,
                ) = values


def test_restock_batch_rejection_creates_no_purchase_orders() -> None:
    query = """
        SELECT id AS product_id, name, stock_quantity, reorder_threshold
        FROM products
        WHERE stock_quantity < reorder_threshold
        ORDER BY id
    """
    with Session(engine) as session:
        before_ids = set(session.scalars(select(PurchaseOrder.id)))

    nodes = QueryThenActionNodes("create_purchase_order", query)
    graph = build_erp_graph(nodes)
    config = create_thread_config(f"restock-rejected-{uuid4()}")
    interrupted = run_erp_graph(
        "Replenish all products whose inventory is below its threshold.",
        graph,
        config,
    )

    approval = get_interrupt_payload(interrupted)
    assert approval is not None
    assert approval["action_count"] > 0
    completed = resume_erp_graph(graph, config, approved=False)

    with Session(engine) as session:
        after_ids = set(session.scalars(select(PurchaseOrder.id)))
    assert after_ids == before_ids
    assert completed["action_results"] == []
    assert completed["approval_status"] == "rejected"


def test_overdue_reminder_rejection_leaves_invoice_unchanged() -> None:
    with Session(engine) as session:
        invoice = session.scalar(select(Invoice).order_by(Invoice.id))
        assert invoice is not None
        invoice_id = invoice.id
        original_values = (
            invoice.amount,
            invoice.status,
            invoice.due_date,
            invoice.reminder_sent_at,
        )

    try:
        with Session(admin_engine) as session, session.begin():
            invoice = session.get(Invoice, invoice_id)
            assert invoice is not None
            invoice.amount = Decimal("9000.00")
            invoice.status = "overdue"
            invoice.due_date = date.today() - timedelta(days=30)
            invoice.reminder_sent_at = None

        query = f"""
            SELECT id AS invoice_id, amount, due_date, status, reminder_sent_at
            FROM invoices
            WHERE id = {invoice_id}
              AND status = 'overdue'
              AND due_date < CURRENT_DATE
              AND amount > 5000
              AND reminder_sent_at IS NULL
        """
        nodes = QueryThenActionNodes("mark_invoice_reminder_sent", query)
        graph = build_erp_graph(nodes)
        config = create_thread_config(f"reminders-rejected-{uuid4()}")
        interrupted = run_erp_graph(
            "Send reminders for all overdue invoices over $5,000.", graph, config
        )

        assert get_interrupt_payload(interrupted) is not None
        completed = resume_erp_graph(graph, config, approved=False)

        with Session(engine) as session:
            assert session.get(Invoice, invoice_id).reminder_sent_at is None
        assert completed["action_results"] == []
        assert completed["approval_status"] == "rejected"
    finally:
        with Session(admin_engine) as session, session.begin():
            invoice = session.get(Invoice, invoice_id)
            assert invoice is not None
            (
                invoice.amount,
                invoice.status,
                invoice.due_date,
                invoice.reminder_sent_at,
            ) = original_values
