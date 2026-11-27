"""LangChain SQL agent powered by Groq-hosted language models."""

from __future__ import annotations

from typing import Any, Optional, Union

# Load .env before LangChain initializes its environment-driven tracers.
from config import GROQ_API_KEY, GROQ_MODEL

from langchain_classic.agents import AgentExecutor
from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain_groq import ChatGroq

from database import readonly_engine
from tools.sql_tools import (
    ReadOnlySQLExecutionError,
    ReadOnlySQLValidationError,
    execute_readonly_sql,
)
from tools.proposal_tools import (
    ACTION_PROPOSAL_TOOL_NAMES,
    PendingAction,
    get_action_proposal_tools,
    parse_pending_action,
)


ERP_TABLES = [
    "customers",
    "products",
    "orders",
    "order_items",
    "invoices",
    "purchase_orders",
]

SALES_GUIDANCE = (
    "products.price is the selling price and products.unit_cost is the supplier "
    "cost. Use price or order_items.unit_price for sales and revenue, and "
    "unit_cost only for purchasing cost. Customer spending, sales, and revenue "
    "exclude cancelled orders (orders.status = 'cancelled')."
)

SQL_AGENT_PROMPT = """You are a careful ERP assistant working with a {dialect}
database. Handle both information requests and business-action requests.

For an information or query request:
1. Call sql_db_list_tables to inspect the available tables.
2. Call sql_db_schema for only the tables relevant to the question.
3. Write one syntactically correct PostgreSQL SELECT query. Unless the user asks
   for a different amount, return no more than {top_k} rows.
4. Always call sql_db_query_checker with the query before execution.
5. Execute the checked query with sql_db_query.
6. If execution returns an error, inspect the error, inspect schemas again when
   useful, correct the query, check it again, and retry.
7. Answer naturally and concisely. Do not invent facts that were not in results.

For a business-action request:
1. Use the read-only SQL tools first if you need to confirm identifiers, current
   state, or the factual reason for the action.
2. Call exactly one matching business-action proposal tool with structured
   arguments and a concise reason. These tools only create pending proposals.
3. Never say the action was completed. Clearly say it is pending approval and
   summarize what would change.
4. Do not call a proposal tool when the user is only asking an information query.

Security rules are absolute:
- Generate and execute exactly one SELECT statement at a time.
- Never generate or execute INSERT, UPDATE, DELETE, DROP, ALTER, CREATE,
  TRUNCATE, GRANT, REVOKE, or any other state-changing statement.
- Never bypass the tools or claim that a pending write succeeded.
- The tools create_purchase_order, cancel_order, update_order_status, and
  mark_invoice_reminder_sent are proposal-only. They do not execute actions.
- Do not use a SELECT statement to invoke functions with side effects.

If a question is unrelated to this ERP database, say that you can only help
with ERP information and ERP action proposals.

""" + SALES_GUIDANCE


class ReadOnlySQLDatabase(SQLDatabase):
    """SQLDatabase adapter whose model-generated query path is read-only."""

    def run(
        self,
        command: Any,
        fetch: str = "all",
        include_columns: bool = False,
        *,
        parameters: Optional[dict[str, Any]] = None,
        execution_options: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Execute model-generated SQL through the project's safety boundary."""
        if not isinstance(command, str):
            raise ReadOnlySQLValidationError("Only SQL strings are accepted.")
        if parameters:
            raise ReadOnlySQLValidationError(
                "Parameterized commands are not supported by this agent tool."
            )

        rows = execute_readonly_sql(command)
        if fetch == "one":
            rows = rows[:1]
        if include_columns:
            return rows
        return str([tuple(row.values()) for row in rows]) if rows else ""

    def run_no_throw(
        self,
        command: str,
        fetch: str = "all",
        include_columns: bool = False,
        *,
        parameters: Optional[dict[str, Any]] = None,
        execution_options: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Return query errors to the model so it can inspect and correct them."""
        try:
            return self.run(
                command,
                fetch=fetch,
                include_columns=include_columns,
                parameters=parameters,
                execution_options=execution_options,
            )
        except (ReadOnlySQLValidationError, ReadOnlySQLExecutionError) as error:
            return f"Error: {error}"


def create_readonly_database() -> ReadOnlySQLDatabase:
    """Create the LangChain SQLDatabase view over the six ERP tables."""
    return ReadOnlySQLDatabase(
        readonly_engine,
        schema="public",
        include_tables=ERP_TABLES,
        # No sample rows: they send customer records to the model on every question,
        # and the schema's CHECK constraints already list every allowed status.
        sample_rows_in_table_info=0,
    )


def create_groq_model(model: Optional[str] = None) -> ChatGroq:
    """Create the configured Groq chat model used by agents and graphs."""
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to the project's .env file."
        )
    return ChatGroq(
        model=model or GROQ_MODEL,
        api_key=GROQ_API_KEY,
        temperature=0,
        max_retries=2,
    )


def create_erp_sql_agent(
    *,
    llm: Optional[Any] = None,
    model: Optional[str] = None,
    verbose: bool = False,
) -> AgentExecutor:
    """Build a query-and-proposal agent using Groq unless an LLM is injected."""
    if llm is None:
        llm = create_groq_model(model)

    database = create_readonly_database()
    toolkit = SQLDatabaseToolkit(db=database, llm=llm)
    proposal_tools = get_action_proposal_tools()

    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        agent_type="tool-calling",
        extra_tools=proposal_tools,
        prefix=SQL_AGENT_PROMPT,
        top_k=10,
        max_iterations=10,
        verbose=verbose,
        agent_executor_kwargs={
            "handle_parsing_errors": True,
            "return_intermediate_steps": True,
        },
    )


def ask_erp(
    question: str,
    *,
    verbose: bool = False,
    agent: Optional[AgentExecutor] = None,
) -> Union[str, PendingAction]:
    """Return a query answer or an authoritative structured pending action."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Question must be a non-empty string.")

    executor = agent or create_erp_sql_agent(verbose=verbose)
    result = executor.invoke({"input": question.strip()})

    for action, observation in result.get("intermediate_steps", []):
        if action.tool in ACTION_PROPOSAL_TOOL_NAMES:
            return parse_pending_action(observation)

    return str(result["output"])
