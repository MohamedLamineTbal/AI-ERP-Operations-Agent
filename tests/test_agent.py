"""Tests for agent construction and its enforced read-only SQL path."""

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_groq import ChatGroq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent import (
    ReadOnlySQLDatabase,
    ask_erp,
    create_erp_sql_agent,
    create_readonly_database,
)
from database import engine
from models import PurchaseOrder
from tools.proposal_tools import PendingAction


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Deterministic chat model that supports LangChain tool binding in tests."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolCallingFakeModel":
        return self


def test_readonly_database_executes_select() -> None:
    database = create_readonly_database()
    result = database.run("SELECT name FROM products ORDER BY id LIMIT 1")
    assert "Ergonomic Office Chair" in result


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM customers",
        "UPDATE products SET stock_quantity = 0",
        "DROP TABLE orders",
        "SELECT pg_notify('erp_events', 'changed')",
    ],
)
def test_generated_sql_cannot_modify_database_state(query: str) -> None:
    database = create_readonly_database()
    before = database.run(
        """
        SELECT (SELECT COUNT(*) FROM customers) AS customer_count,
               (SELECT COUNT(*) FROM orders) AS order_count,
               (SELECT SUM(stock_quantity) FROM products) AS total_stock
        """
    )

    error = database.run_no_throw(query)

    after = database.run(
        """
        SELECT (SELECT COUNT(*) FROM customers) AS customer_count,
               (SELECT COUNT(*) FROM orders) AS order_count,
               (SELECT SUM(stock_quantity) FROM products) AS total_stock
        """
    )
    assert error.startswith("Error:")
    assert after == before


def test_agent_uses_tool_calling_sql_toolkit() -> None:
    llm = ChatGroq(
        model="openai/gpt-oss-120b", api_key="test-key", temperature=0
    )
    agent = create_erp_sql_agent(llm=llm)

    tools = {tool.name: tool for tool in agent.tools}
    assert set(tools) == {
        "sql_db_query",
        "sql_db_schema",
        "sql_db_list_tables",
        "sql_db_query_checker",
        "create_purchase_order",
        "cancel_order",
        "update_order_status",
        "mark_invoice_reminder_sent",
    }
    assert isinstance(tools["sql_db_query"].db, ReadOnlySQLDatabase)


def test_query_request_uses_sql_tools() -> None:
    query = (
        "SELECT name FROM products "
        "WHERE stock_quantity < reorder_threshold ORDER BY id LIMIT 10"
    )
    llm = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "sql_db_list_tables", "args": {}, "id": "call-1"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "sql_db_schema",
                        "args": {"table_names": "products"},
                        "id": "call-2",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "sql_db_query_checker",
                        "args": {"query": query},
                        "id": "call-3",
                    }
                ],
            ),
            AIMessage(content=query),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "sql_db_query", "args": {"query": query}, "id": "call-4"}
                ],
            ),
            AIMessage(content="Several products need restocking."),
        ]
    )
    agent = create_erp_sql_agent(llm=llm)

    result = agent.invoke({"input": "Which products need restocking?"})

    used_tools = [action.tool for action, _ in result["intermediate_steps"]]
    assert used_tools == [
        "sql_db_list_tables",
        "sql_db_schema",
        "sql_db_query_checker",
        "sql_db_query",
    ]
    assert not {"create_purchase_order", "cancel_order"}.intersection(used_tools)


def test_action_request_returns_structured_proposal_without_execution() -> None:
    with Session(engine) as session:
        before_count = session.scalar(select(func.count(PurchaseOrder.id)))

    llm = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_purchase_order",
                        "args": {
                            "product_id": 7,
                            "quantity": 20,
                            "reason": "inventory below threshold",
                        },
                        "id": "call-action-1",
                    }
                ],
            ),
            AIMessage(content="The purchase order is pending approval."),
        ]
    )
    agent = create_erp_sql_agent(llm=llm)

    result = ask_erp(
        "Create a purchase order for 20 units of product 7.", agent=agent
    )

    assert isinstance(result, PendingAction)
    assert result.tool_name == "create_purchase_order"
    assert result.arguments == {"product_id": 7, "quantity": 20}
    assert result.reason == "inventory below threshold"
    assert "draft purchase_orders row" in result.what_will_change

    with Session(engine) as session:
        after_count = session.scalar(select(func.count(PurchaseOrder.id)))
    assert after_count == before_count
