"""Routing tests for the ERP LangGraph StateGraph."""

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from graph import (
    AgentState,
    ERPGraphNodes,
    build_erp_graph,
    create_thread_config,
    get_interrupt_payload,
    resume_erp_graph,
    run_erp_graph,
)
from tools.proposal_tools import PendingAction, format_pending_action
from tools.sql_tools import validate_readonly_sql


class UnexpectedLLM:
    def with_structured_output(self, schema: Any) -> Any:
        raise AssertionError("Known query-then-action intents should be deterministic")


class BrokenStructuredOutputLLM:
    def with_structured_output(self, schema: Any) -> Any:
        raise RuntimeError("structured output unavailable")


class BrokenInvokeLLM:
    def invoke(self, messages: Any) -> Any:
        raise RuntimeError("answer model unavailable")


@pytest.mark.parametrize(
    "question,action_tool",
    [
        ("Replenish all low-stock products.", "create_purchase_order"),
        (
            "Mark a reminder sent for each overdue invoice over $5,000.",
            "mark_invoice_reminder_sent",
        ),
    ],
)
def test_bulk_business_requests_require_query_then_action(
    question: str, action_tool: str
) -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = UnexpectedLLM()

    result = nodes.understand_request({"messages": [HumanMessage(content=question)]})

    assert result == {
        "request_type": "query_action",
        "action_tool": action_tool,
        "error": None,
    }


def test_request_classification_setup_failure_becomes_graph_error() -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = BrokenStructuredOutputLLM()

    result = nodes.understand_request(
        {"messages": [HumanMessage(content="Show recent orders.")]}
    )

    assert result == {
        "error": "Request classification failed: structured output unavailable"
    }


def test_answer_generation_failure_becomes_graph_error() -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = BrokenInvokeLLM()

    result = nodes.answer_query(
        {
            "messages": [HumanMessage(content="Show recent orders.")],
            "generated_sql": "SELECT id FROM orders",
            "sql_result": [{"id": 1}],
            "error": None,
        }
    )

    assert result["error"] == "Answer generation failed: answer model unavailable"
    assert result["answer"] == result["error"]


class RecordingNodes:
    def __init__(self, request_type: str) -> None:
        self.request_type = request_type
        self.calls: list[str] = []

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("understand_request")
        return {
            "request_type": self.request_type,
            "action_tool": (
                "create_purchase_order"
                if self.request_type == "query_action"
                else None
            ),
            "error": None,
        }

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("generate_sql")
        return {"generated_sql": "SELECT name FROM products", "error": None}

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("check_sql")
        return {"generated_sql": state["generated_sql"], "error": None}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("execute_sql")
        return {"sql_result": [{"name": "LED Desk Lamp"}], "error": None}

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("answer_query")
        answer = "query failed" if state.get("error") else "LED Desk Lamp"
        return {"answer": answer, "messages": [AIMessage(content=answer)]}

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("prepare_action")
        proposal = PendingAction(
            tool_name="create_purchase_order",
            arguments={"product_id": 7, "quantity": 20},
            reason="inventory below threshold",
            what_will_change="A draft purchase order would be created.",
        )
        return {"pending_actions": [proposal], "error": None}

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("validate_action")
        return {"pending_actions": state["pending_actions"], "error": None}

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("request_approval")
        return ERPGraphNodes.request_approval(self, state)

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("execute_action")
        return {"action_result": {"ok": True}, "error": None}

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("finish_action")
        if state.get("approval_status") == "rejected":
            answer = "Action rejected. No database changes were made."
        else:
            answer = format_pending_action(state["pending_actions"][0])
        return {"answer": answer, "messages": [AIMessage(content=answer)]}


class SQLRecoveryNodes(RecordingNodes):
    """Exercise graph retries through the real PostgreSQL execution boundary."""

    def __init__(self, queries: list[str]) -> None:
        super().__init__("query")
        self.queries = queries
        self.generation_count = 0

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("generate_sql")
        index = min(self.generation_count, len(self.queries) - 1)
        self.generation_count += 1
        return {"generated_sql": self.queries[index], "error": None}

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("check_sql")
        validate_readonly_sql(state["generated_sql"])
        return {"generated_sql": state["generated_sql"], "error": None}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("execute_sql")
        return ERPGraphNodes.execute_sql(self, state)

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("answer_query")
        answer = state.get("error") or "query recovered"
        return {"answer": answer, "messages": [AIMessage(content=answer)]}


def test_query_request_routes_through_sql_nodes() -> None:
    nodes = RecordingNodes("query")
    result = run_erp_graph("Which products need restocking?", build_erp_graph(nodes))

    assert nodes.calls == [
        "understand_request",
        "generate_sql",
        "check_sql",
        "execute_sql",
        "answer_query",
    ]
    assert result["request_type"] == "query"
    assert result["sql_result"] == [{"name": "LED Desk Lamp"}]
    assert result["pending_actions"] == []
    assert get_interrupt_payload(result) is None


def test_action_request_routes_to_pending_approval() -> None:
    nodes = RecordingNodes("action")
    graph = build_erp_graph(nodes)
    config = create_thread_config("routing-action")
    interrupted = run_erp_graph(
        "Create a purchase order for product 7.", graph, config
    )

    assert nodes.calls == [
        "understand_request",
        "prepare_action",
        "validate_action",
        "request_approval",
    ]
    assert get_interrupt_payload(interrupted)["tool_name"] == "create_purchase_order"
    assert "execute_sql" not in nodes.calls

    result = resume_erp_graph(graph, config, approved=False)
    assert result["request_type"] == "action"
    assert result["pending_actions"][0].tool_name == "create_purchase_order"
    assert result["approval_status"] == "rejected"
    assert "No database changes" in result["answer"]
    assert "execute_action" not in nodes.calls


def test_query_then_action_routes_through_sql_before_pending_approval() -> None:
    nodes = RecordingNodes("query_action")
    graph = build_erp_graph(nodes)
    config = create_thread_config("routing-query-action")

    interrupted = run_erp_graph(
        "Replenish all low-stock products.", graph, config
    )

    assert nodes.calls == [
        "understand_request",
        "generate_sql",
        "check_sql",
        "execute_sql",
        "prepare_action",
        "validate_action",
        "request_approval",
    ]
    assert interrupted["sql_result"] == [{"name": "LED Desk Lamp"}]
    assert get_interrupt_payload(interrupted)["action_count"] == 1
    assert "answer_query" not in nodes.calls
    assert "execute_action" not in nodes.calls


def test_invalid_sql_is_recovered_and_retried() -> None:
    nodes = SQLRecoveryNodes(
        [
            "SELECT id, FROM customers",
            "SELECT COUNT(*) AS customer_count FROM customers",
        ]
    )
    result = run_erp_graph("Count customers.", build_erp_graph(nodes))

    assert nodes.calls.count("generate_sql") == 2
    assert nodes.calls.count("check_sql") == 2
    assert nodes.calls.count("execute_sql") == 2
    assert result["retry_count"] == 1
    assert result["error"] is None
    assert result["sql_result"][0]["customer_count"] >= 20
    assert result["answer"] == "query recovered"


def test_invalid_sql_stops_after_three_attempts() -> None:
    nodes = SQLRecoveryNodes(["SELECT id, FROM customers"])
    result = run_erp_graph("Show customers.", build_erp_graph(nodes))

    assert nodes.calls.count("execute_sql") == 3
    assert result["retry_count"] == 3
    assert "PostgreSQL could not execute" in result["error"]
    assert result["answer"] == result["error"]
