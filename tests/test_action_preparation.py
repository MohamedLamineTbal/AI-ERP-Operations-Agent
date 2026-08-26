"""Regression tests for reliable structured action preparation."""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph import ERPGraphNodes
from tools.proposal_tools import PendingAction


class RequiredToolModel:
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.binding_kwargs: dict[str, Any] = {}

    def bind_tools(self, tools: Any, **kwargs: Any) -> "RequiredToolModel":
        self.binding_kwargs = kwargs
        return self

    def invoke(self, messages: Any) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": self.arguments,
                    "id": "proposal-call",
                }
            ],
        )


class NarratingToolModel:
    """Represents a model/provider combination that ignores structured tools."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "NarratingToolModel":
        return self

    def invoke(self, messages: Any) -> AIMessage:
        return AIMessage(content="I prepared the requested action.")


@pytest.mark.parametrize(
    "question,tool_name,arguments,expected_arguments",
    [
        (
            "cancel order 43",
            "cancel_order",
            {"order_id": 43, "reason": "Customer requested cancellation"},
            {"order_id": 43},
        ),
        (
            "create a purchase order for 20 units of product 7",
            "create_purchase_order",
            {
                "product_id": 7,
                "quantity": 20,
                "reason": "Inventory replenishment requested",
            },
            {"product_id": 7, "quantity": 20},
        ),
    ],
)
def test_prepare_action_forces_structured_proposal_tool(
    question: str,
    tool_name: str,
    arguments: dict[str, Any],
    expected_arguments: dict[str, Any],
) -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = RequiredToolModel(tool_name, arguments)

    result = nodes.prepare_action({"messages": [HumanMessage(content=question)]})

    assert result["error"] is None
    proposal = result["pending_actions"][0]
    assert isinstance(proposal, PendingAction)
    assert proposal.tool_name == tool_name
    assert proposal.arguments == expected_arguments
    assert nodes.llm.binding_kwargs["tool_choice"] == "required"


@pytest.mark.parametrize(
    "question,tool_name,expected_arguments",
    [
        ("cancel order 42", "cancel_order", {"order_id": 42}),
        (
            "create a purchase order for 20 units of product 7",
            "create_purchase_order",
            {"product_id": 7, "quantity": 20},
        ),
    ],
)
def test_prepare_action_has_typed_fallback_when_model_returns_prose(
    question: str, tool_name: str, expected_arguments: dict[str, Any]
) -> None:
    nodes = ERPGraphNodes.__new__(ERPGraphNodes)
    nodes.llm = NarratingToolModel()

    result = nodes.prepare_action({"messages": [HumanMessage(content=question)]})

    assert result["error"] is None
    proposal = result["pending_actions"][0]
    assert isinstance(proposal, PendingAction)
    assert proposal.tool_name == tool_name
    assert proposal.arguments == expected_arguments
