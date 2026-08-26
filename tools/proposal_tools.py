"""Non-executing LangChain tools that describe pending ERP actions."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field

from tools.action_tools import (
    ActionInput,
    CancelOrderInput,
    CreatePurchaseOrderInput,
    MarkInvoiceReminderSentInput,
    UpdateOrderStatusInput,
)


class PendingAction(ActionInput):
    """Structured description of a proposed, not-yet-executed action."""

    tool_name: str
    arguments: dict[str, Any]
    reason: str = Field(min_length=3, max_length=500)
    what_will_change: str


class ProposeUpdateOrderStatusInput(UpdateOrderStatusInput):
    reason: str = Field(min_length=3, max_length=500)


class ProposeInvoiceReminderInput(MarkInvoiceReminderSentInput):
    reason: str = Field(min_length=3, max_length=500)


def _create_purchase_order_proposal(
    product_id: int, quantity: int, reason: str
) -> str:
    action = CreatePurchaseOrderInput(
        product_id=product_id, quantity=quantity, reason=reason
    )
    return PendingAction(
        tool_name="create_purchase_order",
        arguments={"product_id": action.product_id, "quantity": action.quantity},
        reason=action.reason,
        what_will_change=(
            "A new draft purchase_orders row would be created for "
            f"product {action.product_id} with quantity {action.quantity}."
        ),
    ).model_dump_json()


def _cancel_order_proposal(order_id: int, reason: str) -> str:
    action = CancelOrderInput(order_id=order_id, reason=reason)
    return PendingAction(
        tool_name="cancel_order",
        arguments={"order_id": action.order_id},
        reason=action.reason,
        what_will_change=(
            f"Order {action.order_id} would be set to cancelled and its "
            "cancellation reason would be recorded."
        ),
    ).model_dump_json()


def _update_order_status_proposal(
    order_id: int, new_status: str, reason: str
) -> str:
    action = ProposeUpdateOrderStatusInput(
        order_id=order_id, new_status=new_status, reason=reason
    )
    return PendingAction(
        tool_name="update_order_status",
        arguments={"order_id": action.order_id, "new_status": action.new_status},
        reason=action.reason,
        what_will_change=(
            f"Order {action.order_id} would have its status changed to "
            f"{action.new_status}."
        ),
    ).model_dump_json()


def _mark_invoice_reminder_sent_proposal(invoice_id: int, reason: str) -> str:
    action = ProposeInvoiceReminderInput(invoice_id=invoice_id, reason=reason)
    return PendingAction(
        tool_name="mark_invoice_reminder_sent",
        arguments={"invoice_id": action.invoice_id},
        reason=action.reason,
        what_will_change=(
            f"Invoice {action.invoice_id} would have reminder_sent_at set to "
            "the approval-time timestamp."
        ),
    ).model_dump_json()


def get_action_proposal_tools() -> list[BaseTool]:
    """Return proposal-only tools; none of them performs a database write."""
    return [
        StructuredTool.from_function(
            func=_create_purchase_order_proposal,
            name="create_purchase_order",
            description=(
                "PROPOSAL ONLY: prepare a pending request to create a draft "
                "purchase order. This tool never writes to the database."
            ),
            args_schema=CreatePurchaseOrderInput,
        ),
        StructuredTool.from_function(
            func=_cancel_order_proposal,
            name="cancel_order",
            description=(
                "PROPOSAL ONLY: prepare a pending order cancellation with a "
                "reason. This tool never writes to the database."
            ),
            args_schema=CancelOrderInput,
        ),
        StructuredTool.from_function(
            func=_update_order_status_proposal,
            name="update_order_status",
            description=(
                "PROPOSAL ONLY: prepare a pending order-status change. Include "
                "a concise reason. This tool never writes to the database."
            ),
            args_schema=ProposeUpdateOrderStatusInput,
        ),
        StructuredTool.from_function(
            func=_mark_invoice_reminder_sent_proposal,
            name="mark_invoice_reminder_sent",
            description=(
                "PROPOSAL ONLY: prepare a pending request to mark an invoice "
                "reminder as sent. This tool never writes to the database."
            ),
            args_schema=ProposeInvoiceReminderInput,
        ),
    ]


ACTION_PROPOSAL_TOOL_NAMES = {
    "create_purchase_order",
    "cancel_order",
    "update_order_status",
    "mark_invoice_reminder_sent",
}


ACTION_PREPARATION_PROMPT = """Select exactly one ERP action proposal tool for
the user's request. Extract identifiers and quantities exactly as written by the
user and provide a concise reason. The available tools only construct a pending
proposal; they never modify the database. Do not answer with prose."""


def create_pending_action(
    tool_name: str, arguments: dict[str, Any], tools: list[BaseTool]
) -> PendingAction:
    """Invoke one proposal-only tool and validate its typed result."""
    matching_tool = next((tool for tool in tools if tool.name == tool_name), None)
    if matching_tool is None:
        raise ValueError(f"Unknown proposed action tool: {tool_name}")
    result = matching_tool.invoke(arguments)
    return validate_pending_action(parse_pending_action(result))


def _fallback_action_proposal(question: str, tools: list[BaseTool]) -> PendingAction:
    """Parse the two common action forms when model tool calling is unavailable."""
    cancel_match = re.search(
        r"\bcancel\s+(?:the\s+)?order\s*#?\s*(?P<order_id>\d+)\b",
        question,
        re.IGNORECASE,
    )
    if cancel_match:
        return create_pending_action(
            "cancel_order",
            {
                "order_id": int(cancel_match.group("order_id")),
                "reason": "User requested order cancellation",
            },
            tools,
        )

    purchase_match = re.search(
        r"\bcreate\s+(?:a\s+)?purchase\s+order\s+for\s+"
        r"(?P<quantity>\d+)\s+units?\s+(?:of\s+)?product\s*#?\s*"
        r"(?P<product_id>\d+)\b",
        question,
        re.IGNORECASE,
    )
    if purchase_match:
        return create_pending_action(
            "create_purchase_order",
            {
                "product_id": int(purchase_match.group("product_id")),
                "quantity": int(purchase_match.group("quantity")),
                "reason": "User requested inventory replenishment",
            },
            tools,
        )

    raise ValueError(
        "The model did not produce a valid action proposal, and the request "
        "did not contain a recognizable supported action with its required IDs."
    )


def prepare_action_proposal(question: str, llm: Any) -> PendingAction:
    """Create one validated proposal without executing a business action."""
    tools = get_action_proposal_tools()

    try:
        proposal_model = llm.bind_tools(
            tools,
            tool_choice="required",
            parallel_tool_calls=False,
        )
        response = proposal_model.invoke(
            [
                SystemMessage(content=ACTION_PREPARATION_PROMPT),
                HumanMessage(content=question),
            ]
        )
        tool_calls = getattr(response, "tool_calls", [])
        if len(tool_calls) != 1:
            raise ValueError("The model must produce exactly one action tool call.")
        tool_call = tool_calls[0]
        return create_pending_action(tool_call["name"], tool_call["args"], tools)
    except Exception:
        return _fallback_action_proposal(question, tools)


def parse_pending_action(value: Any) -> PendingAction:
    """Parse a proposal tool result into its structured representation."""
    if isinstance(value, PendingAction):
        return value
    if isinstance(value, str):
        return PendingAction.model_validate_json(value)
    return PendingAction.model_validate(value)


def validate_pending_action(action: PendingAction) -> PendingAction:
    """Revalidate a proposal against the target action's exact input model."""
    values = {**action.arguments, "reason": action.reason}

    if action.tool_name == "create_purchase_order":
        CreatePurchaseOrderInput.model_validate(values)
    elif action.tool_name == "cancel_order":
        CancelOrderInput.model_validate(values)
    elif action.tool_name == "update_order_status":
        UpdateOrderStatusInput.model_validate(action.arguments)
    elif action.tool_name == "mark_invoice_reminder_sent":
        MarkInvoiceReminderSentInput.model_validate(action.arguments)
    else:
        raise ValueError(f"Unknown proposed action tool: {action.tool_name}")

    return action


def format_pending_action(action: PendingAction) -> str:
    """Render a pending action for the terminal without implying execution."""
    lines = ["Pending action:", action.tool_name]
    lines.extend(f"{name}: {value}" for name, value in action.arguments.items())
    lines.append(f"reason: {action.reason}")
    lines.append(f"what will change: {action.what_will_change}")
    return "\n".join(lines)
