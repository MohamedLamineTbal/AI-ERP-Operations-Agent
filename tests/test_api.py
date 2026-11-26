"""HTTP API tests: streamed answers, approvals, and records."""

import json
from typing import Any

import pytest

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import QUERY_FAILED, create_app
from database import engine
from graph import AgentState, ERPGraphNodes, build_erp_graph
from models import Product, PurchaseOrder
from tools.proposal_tools import PendingAction


class QueryNodes:
    def __init__(self, execute_error: Any = None) -> None:
        self.execute_error = execute_error

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        return {"request_type": "query", "error": None}

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        return {"generated_sql": "SELECT name FROM products", "error": None}

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        return {"error": None}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        if self.execute_error:
            return {"error": self.execute_error, "retry_count": state.get("retry_count", 0) + 1}
        return {"sql_result": [{"name": "LED Desk Lamp"}], "error": None}

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.answer_query(self, state) if state.get("error") else {
            "answer": "The LED Desk Lamp.",
            "messages": [AIMessage(content="The LED Desk Lamp.")],
            "error": None,
        }

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        return {"verification": "passed"}

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Questions must not prepare actions")

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Questions must not prepare actions")

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Questions must not prepare actions")

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Questions must not prepare actions")

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        raise AssertionError("Questions must not prepare actions")


class PurchaseOrderNodes(QueryNodes):
    """Deterministic proposal; production validation, approval, and execution."""

    def __init__(self, product_id: int) -> None:
        super().__init__()
        self.product_id = product_id

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        return {"request_type": "action", "error": None}

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        proposal = PendingAction(
            tool_name="create_purchase_order",
            arguments={"product_id": self.product_id, "quantity": 3},
            reason="API approval test",
            what_will_change="A draft purchase order would be created.",
        )
        return {"pending_actions": [proposal], "error": None}

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.validate_action(self, state)

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.request_approval(self, state)

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.execute_action(self, state)

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        return ERPGraphNodes.finish_action(self, state)


ACCESS_KEY = "test-access-key"


def _anonymous_client(nodes: Any) -> TestClient:
    app = create_app(
        build_erp_graph(nodes), access_key=ACCESS_KEY, allowed_hosts=["testserver"]
    )
    return TestClient(app)


def _client(nodes: Any) -> TestClient:
    client = _anonymous_client(nodes)
    assert client.post("/api/session", json={"key": ACCESS_KEY}).status_code == 200
    return client


def _events(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line]


def _first_product() -> Product:
    with Session(engine) as session:
        product = session.scalar(select(Product).order_by(Product.id))
    assert product is not None
    return product


def test_question_streams_progress_then_a_checked_answer() -> None:
    events = _events(_client(QueryNodes()).post("/api/ask", json={"question": "Lowest stock?"}))

    assert [e["label"] for e in events if e["type"] == "step"] == [
        "Understanding the request",
        "Looking up the data",
        "Writing the answer",
        "Checking the answer",
    ]
    assert events[-1] == {
        "type": "answer",
        "answer": "The LED Desk Lamp.",
        "verified": True,
        "rows": [{"name": "LED Desk Lamp"}],
        "truncated": False,
    }


class TruncatedQueryNodes(QueryNodes):
    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        return {"sql_result": [{"name": "LED Desk Lamp"}], "sql_truncated": True, "error": None}


def test_a_cut_off_result_is_flagged_to_the_browser() -> None:
    events = _events(_client(TruncatedQueryNodes()).post("/api/ask", json={"question": "All?"}))

    assert events[-1]["truncated"] is True


def test_failed_question_does_not_expose_internal_errors() -> None:
    nodes = QueryNodes(execute_error='column "revenue" does not exist')
    events = _events(_client(nodes).post("/api/ask", json={"question": "Revenue?"}))

    assert events[-1]["answer"] == QUERY_FAILED
    assert "revenue" not in json.dumps(events[-1])


class UnclassifiedNodes(QueryNodes):
    def understand_request(self, state: AgentState) -> dict[str, Any]:
        return {"error": "Request classification failed: model unavailable"}


def test_request_that_could_not_be_classified_is_not_called_a_change() -> None:
    events = _events(_client(UnclassifiedNodes()).post("/api/ask", json={"question": "Hi"}))

    assert events[-1]["answer"] == QUERY_FAILED


def test_change_waits_for_approval_and_is_applied_with_its_cost() -> None:
    product = _first_product()
    client = _client(PurchaseOrderNodes(product.id))
    approval = _events(client.post("/api/ask", json={"question": "Order 3."}))[-1]

    assert approval["type"] == "approval"
    assert approval["approval"]["total_cost"] == str(product.unit_cost * 3)
    assert [item["id"] for item in client.get("/api/approvals").json()] == [approval["id"]]

    decision = client.post(f"/api/approvals/{approval['id']}", json={"approved": True}).json()
    created_id = decision["results"][0]["id"]
    try:
        assert decision["applied"] is True
        assert client.get("/api/approvals").json() == []
        with Session(engine) as session:
            created = session.get(PurchaseOrder, created_id)
            assert created is not None
            assert (created.quantity, created.unit_cost) == (3, product.unit_cost)
    finally:
        with Session(engine) as session, session.begin():
            session.delete(session.get(PurchaseOrder, created_id))


def test_rejected_change_is_not_applied() -> None:
    client = _client(PurchaseOrderNodes(_first_product().id))
    with Session(engine) as session:
        before = session.scalar(select(func.count(PurchaseOrder.id)))
    approval = _events(client.post("/api/ask", json={"question": "Order 3."}))[-1]

    decision = client.post(f"/api/approvals/{approval['id']}", json={"approved": False}).json()

    assert decision == {"status": "rejected", "applied": False, "results": []}
    with Session(engine) as session:
        assert session.scalar(select(func.count(PurchaseOrder.id))) == before


def test_deciding_an_unknown_approval_is_not_found() -> None:
    response = _client(QueryNodes()).post("/api/approvals/missing", json={"approved": True})

    assert response.status_code == 404


def test_records_cover_every_table() -> None:
    records = _client(QueryNodes()).get("/api/records").json()

    assert set(records) == {"customers", "products", "orders", "invoices", "purchaseOrders"}
    assert all(records[name] for name in records)
    assert records["orders"][0]["items"]
    assert "cost" in records["products"][0]


def test_every_api_route_requires_the_access_key() -> None:
    client = _anonymous_client(QueryNodes())

    assert client.get("/api/records").status_code == 401
    assert client.get("/api/approvals").status_code == 401
    assert client.get("/api/session").status_code == 401
    assert client.post("/api/ask", json={"question": "Lowest stock?"}).status_code == 401
    assert client.post("/api/approvals/any", json={"approved": True}).status_code == 401


def test_a_wrong_access_key_does_not_open_a_session() -> None:
    client = _anonymous_client(QueryNodes())

    assert client.post("/api/session", json={"key": "guess"}).status_code == 401
    assert client.get("/api/records").status_code == 401


def test_requests_for_another_host_are_refused() -> None:
    client = _client(QueryNodes())

    assert client.get("/api/records", headers={"Host": "evil.example.com"}).status_code == 400


def test_the_app_will_not_start_without_an_access_key() -> None:
    with pytest.raises(RuntimeError, match="ERP_ACCESS_KEY"):
        create_app(build_erp_graph(QueryNodes()), access_key="")
