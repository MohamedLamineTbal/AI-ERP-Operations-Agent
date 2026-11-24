"""Human approval tests for state-changing LangGraph actions."""

from typing import Any

from sqlalchemy import func, select
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
from models import Order, PurchaseOrder
from tools.proposal_tools import PendingAction, validate_pending_action


class ApprovalNodes:
    """Deterministic preparation with production approval and execution nodes."""

    def __init__(self, proposal: PendingAction) -> None:
        self.proposal = proposal
        self.execute_calls = 0

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        return {"request_type": "action", "error": None}

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Action requests must not enter the SQL branch")

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Action requests must not enter the SQL branch")

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Action requests must not enter the SQL branch")

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Action requests must not enter the query answer node")

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Action requests must not enter answer verification")

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        return {"pending_actions": [self.proposal], "error": None}

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        proposal = validate_pending_action(state["pending_actions"][0])
        return {"pending_actions": [proposal], "error": None}

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.request_approval(self, state)

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        self.execute_calls += 1
        return ERPGraphNodes.execute_action(self, state)

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.finish_action(self, state)


class BatchApprovalNodes(ApprovalNodes):
    def __init__(self, proposals: list[PendingAction]) -> None:
        self.proposals = proposals
        self.execute_calls = 0

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        return {"pending_actions": self.proposals, "error": None}

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.validate_action(self, state)


def _start_action(proposal: PendingAction, thread_id: str) -> tuple[Any, Any, Any]:
    nodes = ApprovalNodes(proposal)
    graph = build_erp_graph(nodes)
    config = create_thread_config(thread_id)
    interrupted = run_erp_graph("Perform the requested action.", graph, config)
    assert get_interrupt_payload(interrupted)["tool_name"] == proposal.tool_name
    return nodes, graph, config


def _cancellation_proposal(order_id: int) -> PendingAction:
    return PendingAction(
        tool_name="cancel_order",
        arguments={"order_id": order_id},
        reason="Customer requested cancellation",
        what_will_change=f"Order {order_id} would be cancelled.",
    )


def _purchase_order_proposal(product_id: int) -> PendingAction:
    return PendingAction(
        tool_name="create_purchase_order",
        arguments={"product_id": product_id, "quantity": 20},
        reason="Inventory below threshold",
        what_will_change="A draft purchase order would be created.",
    )


def test_cancellation_approved() -> None:
    with Session(engine) as session:
        order = session.scalar(select(Order).where(Order.status == "pending"))
        assert order is not None
        order_id = order.id
        original_status = order.status
        original_reason = order.cancellation_reason

    nodes, graph, config = _start_action(
        _cancellation_proposal(order_id), "cancel-approved"
    )
    try:
        with Session(engine) as session:
            assert session.get(Order, order_id).status == original_status

        result = resume_erp_graph(graph, config, approved=True)

        with Session(engine) as session:
            cancelled = session.get(Order, order_id)
            assert cancelled is not None
            assert cancelled.status == "cancelled"
            assert cancelled.cancellation_reason == "Customer requested cancellation"
        assert nodes.execute_calls == 1
        assert result["approval_status"] == "approved"
        assert "approved and executed" in result["answer"]
    finally:
        with Session(engine) as session, session.begin():
            order = session.get(Order, order_id)
            assert order is not None
            order.status = original_status
            order.cancellation_reason = original_reason


def test_cancellation_rejected() -> None:
    with Session(engine) as session:
        order = session.scalar(select(Order).where(Order.status == "pending"))
        assert order is not None
        order_id = order.id
        original_status = order.status
        original_reason = order.cancellation_reason

    nodes, graph, config = _start_action(
        _cancellation_proposal(order_id), "cancel-rejected"
    )
    result = resume_erp_graph(graph, config, approved=False)

    with Session(engine) as session:
        unchanged = session.get(Order, order_id)
        assert unchanged is not None
        assert unchanged.status == original_status
        assert unchanged.cancellation_reason == original_reason
    assert nodes.execute_calls == 0
    assert result["approval_status"] == "rejected"
    assert "No database changes" in result["answer"]


def test_purchase_order_approved() -> None:
    with Session(engine) as session:
        product_id = session.scalar(
            select(PurchaseOrder.product_id).order_by(PurchaseOrder.product_id)
        )
        before_count = session.scalar(select(func.count(PurchaseOrder.id)))
    assert product_id is not None

    nodes, graph, config = _start_action(
        _purchase_order_proposal(product_id), "purchase-approved"
    )
    result = resume_erp_graph(graph, config, approved=True)
    created_id = result["action_result"]["id"]
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count(PurchaseOrder.id))) == before_count + 1
            created = session.get(PurchaseOrder, created_id)
            assert created is not None
            assert created.quantity == 20
            assert created.reason == "Inventory below threshold"
        assert nodes.execute_calls == 1
    finally:
        with Session(engine) as session, session.begin():
            created = session.get(PurchaseOrder, created_id)
            if created is not None:
                session.delete(created)


def test_purchase_order_rejected_and_database_unchanged() -> None:
    with Session(engine) as session:
        product_id = session.scalar(
            select(PurchaseOrder.product_id).order_by(PurchaseOrder.product_id)
        )
        before_ids = set(session.scalars(select(PurchaseOrder.id)))
    assert product_id is not None

    nodes, graph, config = _start_action(
        _purchase_order_proposal(product_id), "purchase-rejected"
    )
    result = resume_erp_graph(graph, config, approved=False)

    with Session(engine) as session:
        after_ids = set(session.scalars(select(PurchaseOrder.id)))
    assert after_ids == before_ids
    assert nodes.execute_calls == 0
    assert result["action_result"] is None
    assert result["approval_status"] == "rejected"


def test_approved_batch_rolls_back_all_actions_if_one_fails() -> None:
    with Session(engine) as session:
        product_id = session.scalar(select(PurchaseOrder.product_id))
        before_ids = set(session.scalars(select(PurchaseOrder.id)))
    assert product_id is not None

    proposals = [
        PendingAction(
            tool_name="create_purchase_order",
            arguments={"product_id": product_id, "quantity": 10},
            reason="Atomic batch evaluation",
            what_will_change="A draft purchase order would be created.",
        ),
        PendingAction(
            tool_name="create_purchase_order",
            arguments={"product_id": 2_000_000_000, "quantity": 10},
            reason="Atomic batch evaluation",
            what_will_change="A draft purchase order would be created.",
        ),
    ]
    nodes = BatchApprovalNodes(proposals)
    graph = build_erp_graph(nodes)
    config = create_thread_config("atomic-batch-failure")
    interrupted = run_erp_graph("Run the approved batch.", graph, config)

    assert get_interrupt_payload(interrupted)["action_count"] == 2
    result = resume_erp_graph(graph, config, approved=True)

    try:
        with Session(engine) as session:
            after_ids = set(session.scalars(select(PurchaseOrder.id)))
        assert after_ids == before_ids
        assert "Action execution failed" in result["answer"]
    finally:
        with Session(engine) as session, session.begin():
            for purchase_order in session.scalars(
                select(PurchaseOrder).where(PurchaseOrder.id.not_in(before_ids))
            ):
                session.delete(purchase_order)
