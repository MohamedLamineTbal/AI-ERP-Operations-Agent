"""Routing tests for the ERP LangGraph StateGraph."""

from decimal import Decimal
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from graph import (
    MAX_SQL_ATTEMPTS,
    OFF_TOPIC_ANSWER,
    AgentState,
    AnswerReview,
    ERPGraphNodes,
    RequestClassification,
    build_erp_graph,
    cost_pending_actions,
    create_thread_config,
    get_interrupt_payload,
    resume_erp_graph,
    run_erp_graph,
    total_cost,
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


class OffTopicClassifierLLM:
    def with_structured_output(self, schema: Any) -> Any:
        return self

    def invoke(self, messages: Any) -> Any:
        return RequestClassification(request_type="other")


def test_off_topic_request_gets_a_help_reply_without_touching_data() -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = OffTopicClassifierLLM()

    result = run_erp_graph("HI", build_erp_graph(nodes))

    assert result["answer"] == OFF_TOPIC_ANSWER
    assert result.get("error") is None
    assert result.get("generated_sql") is None
    assert result["pending_actions"] == []


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

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("verify_answer")
        return {"verification": "passed"}

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
        "verify_answer",
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


def _proposal(tool_name: str, arguments: dict[str, Any]) -> PendingAction:
    return PendingAction(
        tool_name=tool_name,
        arguments=arguments,
        reason="Cost evaluation",
        what_will_change="Evaluated for cost only.",
    )


def test_purchase_orders_are_priced_at_supplier_cost() -> None:
    proposals = [
        _proposal("create_purchase_order", {"product_id": 1, "quantity": 13}),
        _proposal("cancel_order", {"order_id": 4}),
        _proposal("create_purchase_order", {"product_id": 3, "quantity": 20}),
    ]
    costs = cost_pending_actions(
        proposals, {1: Decimal("144.94"), 3: Decimal("21.97")}
    )

    assert costs == [
        {"unit_cost": "144.94", "line_cost": "1884.22"},
        None,
        {"unit_cost": "21.97", "line_cost": "439.40"},
    ]
    assert total_cost(costs) == "2323.62"


def test_total_cost_is_unknown_when_a_product_has_no_cost() -> None:
    proposals = [
        _proposal("create_purchase_order", {"product_id": 1, "quantity": 2}),
        _proposal("create_purchase_order", {"product_id": 999, "quantity": 2}),
    ]
    costs = cost_pending_actions(proposals, {1: Decimal("10.00")})

    assert costs[1] == {"unit_cost": None, "line_cost": None}
    assert total_cost(costs) is None


def test_actions_without_purchase_orders_have_no_total() -> None:
    costs = cost_pending_actions([_proposal("cancel_order", {"order_id": 4})], {})

    assert costs == [None]
    assert total_cost(costs) is None


class ScriptedReviewer:
    def __init__(self, reviews: list[Any]) -> None:
        self.reviews = reviews

    def with_structured_output(self, schema: Any) -> "ScriptedReviewer":
        return self

    def invoke(self, messages: Any) -> AnswerReview:
        review = self.reviews.pop(0)
        if isinstance(review, Exception):
            raise review
        return review


class ReviewedQueryNodes(RecordingNodes):
    """Fake query steps reviewed by the production verify_answer node."""

    def __init__(self, reviews: list[Any]) -> None:
        super().__init__("query")
        self.llm = ScriptedReviewer(reviews)
        self.sql_corrections: list[Any] = []
        self.answer_feedback: list[Any] = []

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        self.sql_corrections.append(state.get("error"))
        return super().generate_sql(state)

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("answer_query")
        self.answer_feedback.append(state.get("answer_feedback"))
        return {
            "answer": "LED Desk Lamp",
            "messages": [AIMessage(content="LED Desk Lamp")],
            "error": None,
            "answer_attempts": state.get("answer_attempts", 0) + 1,
        }

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        self.calls.append("verify_answer")
        return ERPGraphNodes.verify_answer(self, state)


def test_query_the_reviewer_rejects_is_regenerated_with_its_reason() -> None:
    nodes = ReviewedQueryNodes(
        [
            AnswerReview(
                sql_answers_question=False,
                answer_matches_result=True,
                problem="It lists products, not customers.",
            ),
            AnswerReview(sql_answers_question=True, answer_matches_result=True),
        ]
    )
    result = run_erp_graph("Which customers owe money?", build_erp_graph(nodes))

    assert nodes.calls.count("generate_sql") == 2
    assert nodes.sql_corrections[0] is None
    assert "It lists products, not customers." in nodes.sql_corrections[1]
    assert result["verification"] == "passed"
    assert result["answer"] == "LED Desk Lamp"


def test_query_rejections_share_the_sql_attempt_limit() -> None:
    rejection = AnswerReview(
        sql_answers_question=False,
        answer_matches_result=True,
        problem="Wrong table.",
    )
    nodes = ReviewedQueryNodes([rejection] * MAX_SQL_ATTEMPTS)
    result = run_erp_graph("Which customers owe money?", build_erp_graph(nodes))

    assert nodes.calls.count("generate_sql") == MAX_SQL_ATTEMPTS
    assert result["verification"] == "failed"
    assert result["answer"].startswith("LED Desk Lamp")
    assert "could not verify this answer. Wrong table." in result["answer"]


def test_unsupported_answer_is_rewritten_once_then_flagged() -> None:
    mismatch = AnswerReview(
        sql_answers_question=True,
        answer_matches_result=False,
        problem="The answer names a product missing from the result.",
    )
    nodes = ReviewedQueryNodes([mismatch, mismatch])
    result = run_erp_graph("Which product is lowest?", build_erp_graph(nodes))

    assert nodes.calls.count("generate_sql") == 1
    assert nodes.answer_feedback == [
        None,
        "The answer names a product missing from the result.",
    ]
    assert result["verification"] == "failed"
    assert "could not verify this answer" in result["answer"]


def test_answer_is_kept_when_the_reviewer_is_unavailable() -> None:
    nodes = ReviewedQueryNodes([RuntimeError("reviewer offline")])
    result = run_erp_graph("Which product is lowest?", build_erp_graph(nodes))

    assert result["verification"] == "unavailable"
    assert result["answer"] == "LED Desk Lamp"
