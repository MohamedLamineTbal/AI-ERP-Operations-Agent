"""LangGraph workflow for ERP queries and pending business actions."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from uuid import uuid4
from typing import Any, Literal, Optional

# Load .env before LangChain and LangGraph initialize environment-driven tracers.
import config as _config

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing_extensions import Annotated, TypedDict

from agent import SALES_GUIDANCE, create_groq_model, create_readonly_database
from database import engine
from tools.proposal_tools import (
    PendingAction,
    create_pending_action,
    get_action_proposal_tools,
    prepare_action_proposal,
    validate_pending_action,
)
from tools.sql_tools import execute_readonly_sql, validate_readonly_sql
from tools.action_tools import (
    cancel_order,
    create_purchase_order,
    get_unit_costs,
    mark_invoice_reminder_sent,
    update_order_status,
)


MAX_SQL_ATTEMPTS = 3
MAX_ANSWER_ATTEMPTS = 2
logger = logging.getLogger("erp_agent")


def configure_outcome_log() -> None:
    """Write the erp_agent log, including record_outcome lines, to erp_agent.log."""
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    handler = logging.FileHandler(_config.BASE_DIR / "erp_agent.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


VERIFY_ANSWER_PROMPT = (
    "You review an ERP assistant's work before the user sees it. Given the "
    "question, the SQL that ran, its result rows, and the drafted answer, decide: "
    "sql_answers_question: does the SQL retrieve what the question asks for, with "
    "the right tables, filters, and aggregation? "
    "answer_matches_result: is every fact and number in the answer supported by "
    "the result rows? Simple arithmetic on the rows counts as supported, and an "
    "empty result can be a correct answer. "
    "If either is false, explain the problem in one sentence in problem. "
)
ActionToolName = Literal[
    "create_purchase_order",
    "cancel_order",
    "update_order_status",
    "mark_invoice_reminder_sent",
]


class ActionCost(TypedDict):
    """Supplier cost of one proposed purchase order; None when the product is unknown."""

    unit_cost: Optional[str]
    line_cost: Optional[str]


class AgentState(TypedDict, total=False):
    """Shared typed data carried between every ERP graph node."""

    messages: Annotated[list[AnyMessage], add_messages]
    request_type: Literal["query", "action", "query_action"]
    action_tool: Optional[ActionToolName]
    generated_sql: Optional[str]
    sql_result: Optional[list[dict[str, Any]]]
    pending_actions: list[PendingAction]
    action_costs: list[Optional[ActionCost]]
    error: Optional[str]
    retry_count: int
    answer: Optional[str]
    answer_attempts: int
    answer_feedback: Optional[str]
    verification: Optional[
        Literal["passed", "retry_sql", "retry_answer", "failed", "unavailable"]
    ]
    approval_status: Optional[Literal["pending", "approved", "rejected"]]
    action_result: Optional[dict[str, Any]]
    action_results: list[dict[str, Any]]


class AnswerReview(BaseModel):
    sql_answers_question: bool
    answer_matches_result: bool
    problem: Optional[str] = None


class RequestClassification(BaseModel):
    request_type: Literal["query", "action", "query_action"]
    action_tool: Optional[ActionToolName] = None


def _infer_query_action(question: str) -> Optional[ActionToolName]:
    """Recognize supported set-based actions that require selecting rows first."""
    normalized = question.lower()
    if not re.search(r"\b(?:all|every|each)\b", normalized):
        return None
    if re.search(r"\b(?:restock|replenish|reorder)\w*\b", normalized) and re.search(
        r"\b(?:product|item|inventory|stock)\w*\b", normalized
    ):
        return "create_purchase_order"
    if re.search(r"\bremind\w*\b", normalized) and re.search(
        r"\binvoice\w*\b", normalized
    ):
        return "mark_invoice_reminder_sent"
    return None


def _question_from_state(state: AgentState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("The graph state does not contain a user question.")


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return str(content)


def _extract_sql(value: Any) -> str:
    text_value = _message_text(value).strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text_value, re.I | re.S)
    if fenced:
        text_value = fenced.group(1).strip()
    if text_value.lower().startswith("sqlquery:"):
        text_value = text_value.split(":", 1)[1].strip()
    return text_value


def cost_pending_actions(
    proposals: list[PendingAction], unit_costs: dict[int, Decimal]
) -> list[Optional[ActionCost]]:
    """Price each proposed purchase order; other actions carry no cost."""
    costs: list[Optional[ActionCost]] = []
    for proposal in proposals:
        if proposal.tool_name != "create_purchase_order":
            costs.append(None)
            continue
        unit_cost = unit_costs.get(proposal.arguments["product_id"])
        if unit_cost is None:
            costs.append({"unit_cost": None, "line_cost": None})
            continue
        costs.append(
            {
                "unit_cost": str(unit_cost),
                "line_cost": str(unit_cost * proposal.arguments["quantity"]),
            }
        )
    return costs


def total_cost(costs: list[Optional[ActionCost]]) -> Optional[str]:
    """Sum the priced lines, or None when any line's cost is unknown."""
    lines = [cost for cost in costs if cost is not None]
    if not lines or any(cost["line_cost"] is None for cost in lines):
        return None
    return str(sum(Decimal(cost["line_cost"]) for cost in lines))


def _execute_business_action(
    proposal: PendingAction, session: Session
) -> dict[str, Any]:
    """Execute one already-approved and validated business action."""
    arguments = proposal.arguments
    if proposal.tool_name == "create_purchase_order":
        return create_purchase_order(
            product_id=arguments["product_id"],
            quantity=arguments["quantity"],
            reason=proposal.reason,
            _session=session,
        )
    if proposal.tool_name == "cancel_order":
        return cancel_order(
            order_id=arguments["order_id"],
            reason=proposal.reason,
            _session=session,
        )
    if proposal.tool_name == "update_order_status":
        return update_order_status(
            order_id=arguments["order_id"],
            new_status=arguments["new_status"],
            _session=session,
        )
    if proposal.tool_name == "mark_invoice_reminder_sent":
        return mark_invoice_reminder_sent(
            invoice_id=arguments["invoice_id"], _session=session
        )
    raise ValueError(f"Unknown action tool: {proposal.tool_name}")


class ERPGraphNodes:
    """Production node implementations composed from existing ERP components."""

    def __init__(self, llm: Optional[Any] = None) -> None:
        self.llm = llm or create_groq_model()
        self.database = create_readonly_database()
        toolkit = SQLDatabaseToolkit(db=self.database, llm=self.llm)
        self.query_checker = next(
            tool for tool in toolkit.get_tools() if tool.name == "sql_db_query_checker"
        )

    def understand_request(self, state: AgentState) -> dict[str, Any]:
        """Classify the request as an information query or business action."""
        question = _question_from_state(state)
        inferred_action = _infer_query_action(question)
        if inferred_action:
            return {
                "request_type": "query_action",
                "action_tool": inferred_action,
                "error": None,
            }
        try:
            classifier = self.llm.with_structured_output(RequestClassification)
            result = classifier.invoke(
                [
                    SystemMessage(
                        content=(
                            "Classify an ERP request. Use 'query' for inspection only. "
                            "Use 'action' for one directly identified business record. "
                            "Use 'query_action' when records must first be selected from "
                            "the database before applying an action to the matching set. "
                            "For action requests, set action_tool to the matching tool."
                        )
                    ),
                    HumanMessage(content=question),
                ]
            )
            return {
                "request_type": result.request_type,
                "action_tool": result.action_tool,
                "error": None,
            }
        except Exception as error:
            return {"error": f"Request classification failed: {error}"}

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        """Generate or correct one PostgreSQL SELECT using current schema context."""
        question = _question_from_state(state)
        previous_sql = state.get("generated_sql")
        previous_error = state.get("error")
        correction_context = ""
        if previous_error:
            correction_context = (
                f"\nPrevious SQL: {previous_sql or '(none)'}"
                f"\nPostgreSQL/validation error: {previous_error}"
                "\nCorrect that failure in the next query."
            )

        selection_requirements = ""
        if state.get("request_type") == "query_action":
            if state.get("action_tool") == "create_purchase_order":
                selection_requirements = (
                    "\nThis SELECT identifies products for purchase-order proposals. "
                    "It must return products.id AS product_id, stock_quantity, and "
                    "reorder_threshold for every matching product."
                )
            elif state.get("action_tool") == "mark_invoice_reminder_sent":
                selection_requirements = (
                    "\nThis SELECT identifies invoices for reminder proposals. It "
                    "must return invoices.id AS invoice_id, amount, due_date, status, "
                    "and reminder_sent_at for every matching invoice. Treat overdue "
                    "as status = 'overdue' and due_date < CURRENT_DATE. Exclude rows "
                    "whose reminder_sent_at is already set. Apply any amount condition "
                    "from the user's request."
                )

        try:
            response = self.llm.invoke(
                [
                    SystemMessage(
                        content=(
                            "Generate exactly one PostgreSQL SELECT statement for the "
                            "question. Return SQL only, without commentary. Never use "
                            "INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, "
                            "GRANT, REVOKE, SELECT INTO, or side-effecting functions. "
                            f"{SALES_GUIDANCE}"
                        )
                    ),
                    HumanMessage(
                        content=(
                            f"Question: {question}\n"
                            f"Available tables: {', '.join(self.database.get_usable_table_names())}\n"
                            f"Schema:\n{self.database.get_table_info()}"
                            f"{selection_requirements}"
                            f"{correction_context}"
                        )
                    ),
                ]
            )
            return {"generated_sql": _extract_sql(response), "error": None}
        except Exception as error:
            return {"error": f"SQL generation failed: {error}"}

    def check_sql(self, state: AgentState) -> dict[str, Any]:
        """Use LangChain's SQL checker and the strict read-only validator."""
        query = state.get("generated_sql")
        try:
            if not query:
                raise ValueError(state.get("error") or "No SQL query was generated.")
            checked_query = _extract_sql(self.query_checker.invoke({"query": query}))
            validate_readonly_sql(checked_query)
            return {"generated_sql": checked_query, "error": None}
        except Exception as error:
            return {
                "error": f"SQL check failed: {error}",
                "retry_count": state.get("retry_count", 0) + 1,
            }

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        """Execute checked SQL and capture errors for graph-controlled retries."""
        try:
            result = execute_readonly_sql(state["generated_sql"])
            return {"sql_result": result, "error": None}
        except Exception as error:
            return {
                "sql_result": None,
                "error": str(error),
                "retry_count": state.get("retry_count", 0) + 1,
            }

    def answer_query(self, state: AgentState) -> dict[str, Any]:
        """Turn SQL results into a natural answer, or report the final error."""
        answer_error = state.get("error")
        if answer_error:
            answer = (
                f"I could not complete the query after "
                f"{state.get('retry_count', 0)} attempts. "
                f"Last error: {answer_error}"
            )
        else:
            question = _question_from_state(state)
            feedback = state.get("answer_feedback")
            correction = (
                f"\nA reviewer rejected your previous answer: {feedback}"
                "\nWrite a corrected answer."
                if feedback
                else ""
            )
            try:
                response = self.llm.invoke(
                    [
                        SystemMessage(
                            content=(
                                "Answer the ERP question naturally and concisely using "
                                "only the supplied SQL result."
                            )
                        ),
                        HumanMessage(
                            content=(
                                f"Question: {question}\n"
                                f"SQL: {state.get('generated_sql')}\n"
                                f"Result: {json.dumps(state.get('sql_result'), default=str)}"
                                f"{correction}"
                            )
                        ),
                    ]
                )
                answer = _message_text(response)
            except Exception as error:
                answer_error = f"Answer generation failed: {error}"
                answer = answer_error
        return {
            "answer": answer,
            "messages": [AIMessage(content=answer)],
            "error": answer_error,
            "answer_attempts": state.get("answer_attempts", 0) + 1,
        }

    def verify_answer(self, state: AgentState) -> dict[str, Any]:
        """Check the query and the drafted answer, and decide whether to retry."""
        try:
            review = self.llm.with_structured_output(AnswerReview).invoke(
                [
                    SystemMessage(
                        content=VERIFY_ANSWER_PROMPT + SALES_GUIDANCE
                    ),
                    HumanMessage(
                        content=(
                            f"Question: {_question_from_state(state)}\n"
                            f"SQL: {state.get('generated_sql')}\n"
                            f"Result: {json.dumps(state.get('sql_result'), default=str)}\n"
                            f"Answer: {state.get('answer')}"
                        )
                    ),
                ]
            )
        except Exception as error:
            logger.warning("Answer verification unavailable: %s", error)
            return {"verification": "unavailable"}

        if review.sql_answers_question and review.answer_matches_result:
            return {"verification": "passed", "answer_feedback": None}

        problem = review.problem or "The answer does not match the question or result."
        if not review.sql_answers_question:
            retry_count = state.get("retry_count", 0) + 1
            if retry_count < MAX_SQL_ATTEMPTS:
                return {
                    "verification": "retry_sql",
                    "error": f"The query does not answer the question: {problem}",
                    "retry_count": retry_count,
                    "answer_feedback": None,
                }
        elif state.get("answer_attempts", 0) < MAX_ANSWER_ATTEMPTS:
            return {"verification": "retry_answer", "answer_feedback": problem}

        answer = f"{state.get('answer')}\n\nWarning: I could not verify this answer. {problem}"
        return {
            "verification": "failed",
            "answer": answer,
            "messages": [AIMessage(content=answer)],
        }

    def prepare_action(self, state: AgentState) -> dict[str, Any]:
        """Create validated, non-executing structured action proposals."""
        try:
            if state.get("request_type") == "query_action":
                return self._prepare_query_actions(state)
            proposal = prepare_action_proposal(
                _question_from_state(state), llm=self.llm
            )
            return {"pending_actions": [proposal], "error": None}
        except Exception as error:
            return {"pending_actions": [], "error": f"Action preparation failed: {error}"}

    def _prepare_query_actions(self, state: AgentState) -> dict[str, Any]:
        """Transform selected database rows into deterministic proposals."""
        action_tool = state.get("action_tool")
        rows = state.get("sql_result") or []
        tools = get_action_proposal_tools()

        proposals = []
        if action_tool == "create_purchase_order":
            for row in rows:
                product_id = int(row["product_id"])
                stock_quantity = int(row["stock_quantity"])
                reorder_threshold = int(row["reorder_threshold"])
                if stock_quantity >= reorder_threshold:
                    continue
                target_stock = reorder_threshold * 2
                quantity = max(1, target_stock - stock_quantity)
                proposals.append(
                    create_pending_action(
                        "create_purchase_order",
                        {
                            "product_id": product_id,
                            "quantity": quantity,
                            "reason": "Automatic low-stock replenishment",
                        },
                        tools,
                    )
                )
        elif action_tool == "mark_invoice_reminder_sent":
            for row in rows:
                if row.get("reminder_sent_at") is not None:
                    continue
                invoice_id = int(row["invoice_id"])
                proposals.append(
                    create_pending_action(
                        "mark_invoice_reminder_sent",
                        {
                            "invoice_id": invoice_id,
                            "reason": "Automatic overdue-invoice reminder",
                        },
                        tools,
                    )
                )
        else:
            raise ValueError(f"Unsupported query-then-action tool: {action_tool}")
        return {"pending_actions": proposals, "error": None}

    def validate_action(self, state: AgentState) -> dict[str, Any]:
        """Validate the pending proposal without executing its business action."""
        if state.get("error"):
            return {}
        try:
            proposals = state.get("pending_actions", [])
            if not proposals and state.get("request_type") != "query_action":
                raise ValueError("At least one pending action is required.")
            validated = [validate_pending_action(proposal) for proposal in proposals]
            product_ids = [
                proposal.arguments["product_id"]
                for proposal in validated
                if proposal.tool_name == "create_purchase_order"
            ]
            unit_costs = get_unit_costs(product_ids) if product_ids else {}
            return {
                "pending_actions": validated,
                "action_costs": cost_pending_actions(validated, unit_costs),
                "error": None,
            }
        except Exception as error:
            return {"error": f"Action validation failed: {error}"}

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        """Pause before every state-changing action and await a human decision."""
        proposals = state["pending_actions"]
        costs = state.get("action_costs") or [None] * len(proposals)
        actions = [
            {
                "tool_name": proposal.tool_name,
                "arguments": proposal.arguments,
                "reason": proposal.reason,
                "what_will_change": proposal.what_will_change,
                **(cost or {}),
            }
            for proposal, cost in zip(proposals, costs)
        ]
        payload: dict[str, Any] = {
            "type": "action_approval",
            "action_count": len(actions),
            "actions": actions,
        }
        if any(cost is not None for cost in costs):
            payload["total_cost"] = total_cost(costs)
        if len(actions) == 1:
            payload.update(actions[0])
        decision = interrupt(payload)

        if isinstance(decision, str) and decision in {"approved", "rejected"}:
            approved = decision == "approved"
        elif isinstance(decision, dict):
            approved = decision.get("approved")
        else:
            approved = decision
        if not isinstance(approved, bool):
            return {
                "approval_status": "rejected",
                "error": "Approval response must be 'approved' or 'rejected'.",
            }
        return {
            "approval_status": "approved" if approved else "rejected",
            "error": None,
        }

    def execute_action(self, state: AgentState) -> dict[str, Any]:
        """Execute all validated pending actions after explicit approval."""
        try:
            proposals = [
                validate_pending_action(proposal)
                for proposal in state["pending_actions"]
            ]
            with Session(engine) as session, session.begin():
                results = [
                    _execute_business_action(proposal, session)
                    for proposal in proposals
                ]
            return {
                "action_results": results,
                "action_result": results[0] if len(results) == 1 else None,
                "error": None,
            }
        except Exception as error:
            return {
                "action_results": [],
                "action_result": None,
                "error": f"Action execution failed: {error}",
            }

    def finish_action(self, state: AgentState) -> dict[str, Any]:
        """Report approval, rejection, validation failure, or execution failure."""
        if state.get("error"):
            answer = state["error"]
        elif not state.get("pending_actions"):
            answer = "No matching records were found, so no actions were proposed."
        elif state.get("approval_status") == "rejected":
            answer = "Action rejected. No database changes were made."
        elif state.get("approval_status") == "approved":
            proposals = state["pending_actions"]
            if len(proposals) == 1:
                answer = (
                    f"Action approved and executed: {proposals[0].tool_name}\n"
                    f"Result: {json.dumps(state.get('action_result'), default=str)}"
                )
            else:
                answer = (
                    f"{len(proposals)} actions approved and executed.\n"
                    f"Results: {json.dumps(state.get('action_results'), default=str)}"
                )
        else:
            answer = "Action was not executed because no approval decision was recorded."
        return {"answer": answer, "messages": [AIMessage(content=answer)]}


def _route_request(
    state: AgentState,
) -> Literal["query", "action", "query_action", "error"]:
    if state.get("error"):
        return "error"
    return state["request_type"]


def _route_after_sql_step(
    state: AgentState,
) -> Literal["execute", "retry", "answer"]:
    if not state.get("error"):
        return "execute"
    if state.get("retry_count", 0) >= MAX_SQL_ATTEMPTS:
        return "answer"
    return "retry"


def _route_after_execution(
    state: AgentState,
) -> Literal["retry", "answer", "prepare_action"]:
    if state.get("error") and state.get("retry_count", 0) < MAX_SQL_ATTEMPTS:
        return "retry"
    if not state.get("error") and state.get("request_type") == "query_action":
        return "prepare_action"
    return "answer"


def _route_after_answer(state: AgentState) -> Literal["verify", "record"]:
    return "record" if state.get("error") else "verify"


def _route_after_verification(
    state: AgentState,
) -> Literal["retry_sql", "retry_answer", "record"]:
    verification = state.get("verification")
    if verification in {"retry_sql", "retry_answer"}:
        return verification
    return "record"


def record_outcome(state: AgentState) -> dict[str, Any]:
    """Log one structured line describing how the request ended."""
    logger.info(
        json.dumps(
            {
                "question": _question_from_state(state),
                "request_type": state.get("request_type"),
                "sql": state.get("generated_sql"),
                "sql_retries": state.get("retry_count", 0),
                "verification": state.get("verification"),
                "approval_status": state.get("approval_status"),
                "actions": [
                    {"tool_name": proposal.tool_name, "arguments": proposal.arguments}
                    for proposal in state.get("pending_actions", [])
                ],
                "action_results": state.get("action_results"),
                "error": state.get("error"),
                "answer": state.get("answer"),
            },
            default=str,
        )
    )
    return {}


def _route_after_action_validation(
    state: AgentState,
) -> Literal["approval", "finish"]:
    if state.get("error") or not state.get("pending_actions"):
        return "finish"
    return "approval"


def _route_after_approval(
    state: AgentState,
) -> Literal["execute", "finish"]:
    if state.get("approval_status") == "approved" and not state.get("error"):
        return "execute"
    return "finish"


def build_erp_graph(
    nodes: Optional[Any] = None, checkpointer: Optional[Any] = None
) -> Any:
    """Build and compile the ERP StateGraph."""
    graph_nodes = nodes or ERPGraphNodes()
    workflow = StateGraph(AgentState)

    workflow.add_node("understand_request", graph_nodes.understand_request)
    workflow.add_node("generate_sql", graph_nodes.generate_sql)
    workflow.add_node("check_sql", graph_nodes.check_sql)
    workflow.add_node("execute_sql", graph_nodes.execute_sql)
    workflow.add_node("answer_query", graph_nodes.answer_query)
    workflow.add_node("prepare_action", graph_nodes.prepare_action)
    workflow.add_node("validate_action", graph_nodes.validate_action)
    workflow.add_node("request_approval", graph_nodes.request_approval)
    workflow.add_node("execute_action", graph_nodes.execute_action)
    workflow.add_node("finish_action", graph_nodes.finish_action)
    workflow.add_node("verify_answer", graph_nodes.verify_answer)
    workflow.add_node("record_outcome", record_outcome)

    workflow.add_edge(START, "understand_request")
    workflow.add_conditional_edges(
        "understand_request",
        _route_request,
        {
            "query": "generate_sql",
            "query_action": "generate_sql",
            "action": "prepare_action",
            "error": "answer_query",
        },
    )

    workflow.add_edge("generate_sql", "check_sql")
    workflow.add_conditional_edges(
        "check_sql",
        _route_after_sql_step,
        {"execute": "execute_sql", "retry": "generate_sql", "answer": "answer_query"},
    )
    workflow.add_conditional_edges(
        "execute_sql",
        _route_after_execution,
        {
            "retry": "generate_sql",
            "answer": "answer_query",
            "prepare_action": "prepare_action",
        },
    )
    workflow.add_conditional_edges(
        "answer_query",
        _route_after_answer,
        {"verify": "verify_answer", "record": "record_outcome"},
    )
    workflow.add_conditional_edges(
        "verify_answer",
        _route_after_verification,
        {
            "retry_sql": "generate_sql",
            "retry_answer": "answer_query",
            "record": "record_outcome",
        },
    )
    workflow.add_edge("record_outcome", END)

    workflow.add_edge("prepare_action", "validate_action")
    workflow.add_conditional_edges(
        "validate_action",
        _route_after_action_validation,
        {"approval": "request_approval", "finish": "finish_action"},
    )
    workflow.add_conditional_edges(
        "request_approval",
        _route_after_approval,
        {"execute": "execute_action", "finish": "finish_action"},
    )
    workflow.add_edge("execute_action", "finish_action")
    workflow.add_edge("finish_action", "record_outcome")

    return workflow.compile(checkpointer=checkpointer or InMemorySaver())


def create_thread_config(thread_id: Optional[str] = None) -> dict[str, Any]:
    """Create the checkpointer configuration for one resumable graph thread."""
    return {"configurable": {"thread_id": thread_id or str(uuid4())}}


def initial_state(question: str) -> AgentState:
    """Return the starting state for one request."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Question must be a non-empty string.")
    return {
        "messages": [HumanMessage(content=question.strip())],
        "generated_sql": None,
        "sql_result": None,
        "pending_actions": [],
        "action_costs": [],
        "error": None,
        "retry_count": 0,
        "answer": None,
        "answer_attempts": 0,
        "answer_feedback": None,
        "verification": None,
        "approval_status": None,
        "action_result": None,
        "action_results": [],
        "action_tool": None,
    }


def run_erp_graph(
    question: str,
    graph: Optional[Any] = None,
    config: Optional[dict[str, Any]] = None,
) -> AgentState:
    """Run one terminal request through the compiled ERP graph."""
    start = initial_state(question)
    application = graph or build_erp_graph()
    run_config = config or create_thread_config()
    return application.invoke(start, config=run_config)


def resume_erp_graph(
    graph: Any, config: dict[str, Any], approved: bool
) -> AgentState:
    """Resume a paused action thread with an approval or rejection decision."""
    if not isinstance(approved, bool):
        raise ValueError("approved must be a boolean.")
    return graph.invoke(
        Command(resume="approved" if approved else "rejected"),
        config=config,
    )


def get_interrupt_payload(state: AgentState) -> Optional[dict[str, Any]]:
    """Return the first JSON approval payload from an interrupted graph result."""
    interrupts = state.get("__interrupt__", [])  # type: ignore[typeddict-item]
    if not interrupts:
        return None
    return interrupts[0].value
