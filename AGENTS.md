# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt        # deps (use a .venv)
cp .env.example .env                   # then set DATABASE_URL, READONLY_DATABASE_URL, and GROQ_API_KEY
python3 seed.py                        # create tables + the erp_readonly login, seed sample data (skips if customers exist)
uvicorn api:app --reload               # web app at http://127.0.0.1:8000 (Desk + Records)
python3 main.py                        # interactive terminal: one question per run, y/n approval prompt
pytest                                 # full suite
pytest tests/test_graph.py::test_name  # single test
```

There is no lint/format configuration in the repo.

Testing notes:
- `config.py` raises at import time if `DATABASE_URL` or `READONLY_DATABASE_URL` is unset, so almost every module (and test) needs a `.env`.
- `test_database.py`, `test_action_tools.py`, `test_agent.py`, `test_graph_approval.py`, and `test_multi_step_actions.py` hit a real, seeded PostgreSQL database. Action tests write rows and clean up in `finally` blocks.
- Graph tests avoid Groq by injecting fake node classes into `build_erp_graph(nodes=...)` or fake LLMs into `ERPGraphNodes(llm=...)`. Follow that pattern rather than calling the real model.
- `test_tracing_config.py` asserts exact values in `.env.example` and `requirements.txt` (LangSmith tracing off by default, `langsmith>=0.4,<1.0`). Update it if you change either file.

## Architecture

A natural-language ERP assistant over PostgreSQL. Reads go through LLM-generated, read-only SQL. Writes go only through fixed Python business tools, and only after a human approves them.

**Entry paths:** `main.py` (terminal) and `api.py` (FastAPI, serves `web/`) both drive `graph.py` (`build_erp_graph`, `initial_state`, `run_erp_graph`, `resume_erp_graph`). `POST /api/ask` streams NDJSON events: one `step` per graph node (`STEP_LABELS`), then an `answer` or an `approval`. Pending approvals live in memory in `create_app` and are resumed by `POST /api/approvals/{id}`; they are lost when the server restarts. The API never returns internal error text to the browser; details go to the `erp_agent` log. `GET /api/records` returns every table for the Records pages, and `web/app.js` derives summaries, filters, and detail panels from it. The LangGraph `StateGraph` uses an `InMemorySaver` checkpointer. `request_approval` calls `interrupt()`, the caller reads the payload with `get_interrupt_payload`, then resumes on the same `thread_id` with `Command(resume="approved"|"rejected")`.

**Graph flow (`graph.py`):**
- `understand_request` sorts each request into one of four kinds: `query`, `action` (one directly identified record), `query_action` (select matching rows first, then act on every match), or `other` (greetings and off-topic requests, which get the fixed `OFF_TOPIC_ANSWER` and go straight to `record_outcome`). `_infer_query_action` checks with regex before the LLM classifier runs ("restock all low-stock products", "remind all overdue invoices"). Only `create_purchase_order` and `mark_invoice_reminder_sent` support `query_action`.
- SQL loop: `generate_sql` → `check_sql` (LangChain's `sql_db_query_checker` plus `validate_readonly_sql`) → `execute_sql`, with retries capped at `MAX_SQL_ATTEMPTS = 3` via `retry_count`. For `query_action`, `generate_sql` adds required output columns (e.g. `product_id`, `stock_quantity`, `reorder_threshold`). `_prepare_query_actions` then turns the rows into proposals without using the LLM. Purchase quantity = `2 * reorder_threshold - stock_quantity`.
- Self-check: after `answer_query`, `verify_answer` asks the model (structured `AnswerReview`) whether the SQL answers the question and the answer matches the rows. A rejected query goes back to `generate_sql` with the reviewer's reason and uses up the same `MAX_SQL_ATTEMPTS`. A rejected answer is rewritten up to `MAX_ANSWER_ATTEMPTS` times in total. Once those limits are reached, the answer is shown with a warning. If the reviewer call itself fails, the answer is kept and `verification` is `unavailable`. Query-then-action requests skip this step, because a human reviews the slip.
- Every run ends in `record_outcome`, which logs one JSON line to the `erp_agent` logger. `main.py` writes it to `erp_agent.log`. Every fake node class in `tests/` must define each node that `build_erp_graph` registers, including `verify_answer`.
- Action path: `prepare_action` → `validate_action` → `request_approval` → `execute_action` → `finish_action`. `validate_action` is where purchase orders get priced: it looks up `products.unit_cost` and adds per-line and total costs to the approval payload. Keep DB reads out of `request_approval`, because LangGraph re-runs that node from the start when it resumes. `execute_action` re-validates and runs all approved proposals in a single `Session.begin()` transaction, so a batch succeeds or fails as a whole.
- Nodes report failures by writing `state["error"]`; they don't raise. Routing functions (`_route_*`) branch on `error`, `request_type`, `retry_count`, and `approval_status`.

**Three tool layers in `tools/` (keep them separate):**
- `sql_tools.py` is the read-only boundary. A custom tokenizer ignores comments and string literals, then rejects forbidden keywords and side-effecting functions (`FORBIDDEN_KEYWORDS`, `FORBIDDEN_FUNCTIONS`). Execution runs on `readonly_engine` (`READONLY_DATABASE_URL`), a login `seed.py` creates with `SELECT` on the six ERP tables and nothing else, inside `SET TRANSACTION READ ONLY` with a 5s `statement_timeout`, and the transaction always rolls back. That login is the real boundary; the tokenizer is a first filter. Never point it at a superuser or the `DATABASE_URL` login. All model-generated SQL must go through `validate_readonly_sql` / `execute_readonly_sql`.
- `action_tools.py` holds the real mutating functions (ORM-based, Pydantic `*Input` models with `extra="forbid"`, `BusinessActionError` for rule violations). They accept an optional `_session` so the graph can batch them. They are never exposed to the LLM directly.
- `proposal_tools.py` holds the LLM-facing tools with the same names (`create_purchase_order`, `cancel_order`, ...). They only return a `PendingAction` JSON describing `what_will_change`. `prepare_action_proposal` binds them to the LLM, and a regex-based `_fallback_action_proposal` covers cases where the model doesn't call a tool.

Adding a new business action means updating all of: an `*Input` model and function in `action_tools.py`, a proposal tool in `proposal_tools.py`, the `ActionToolName` literal, and `_execute_business_action` in `graph.py`. If the action supports set-based use, also update `_infer_query_action`, the `generate_sql` selection requirements, and `_prepare_query_actions`.

**`agent.py`** contains an older LangChain `create_sql_agent` (`ask_erp`). It is still tested but not used by `main.py`. The graph imports its shared helpers: `create_groq_model` (temperature 0) and `create_readonly_database`, a `SQLDatabase` subclass that sends `run()` through `execute_readonly_sql` and is limited to the six ERP tables in `ERP_TABLES`.

**Data:** `models.py` defines SQLAlchemy 2.0 declarative models for customers, products, orders, order_items, invoices, and purchase_orders. `products.price` is the selling price and `products.unit_cost` the supplier cost. `SALES_GUIDANCE` in `agent.py` tells both SQL prompts which is which, and that customer spending, sales, and revenue exclude cancelled orders (as the Records pages do), and `purchase_orders.unit_cost` records the cost at creation. Valid order statuses are `pending|processing|shipped|completed|cancelled`. New purchase orders are created as `draft`. There are no migrations: `seed.py` calls `Base.metadata.create_all`, so schema changes to existing tables require recreating the database.

**Config:** the LLM is Groq via `langchain-groq` (`GROQ_MODEL`, default `openai/gpt-oss-120b`). LangSmith tracing is optional and controlled by env vars. `config` must be imported before LangChain/LangGraph so `.env` is loaded before tracers initialize. `agent.py` and `graph.py` both do this deliberately.

<!-- skrrt:ship -->
## Git workflow

Use the Skrrt ship skills for git work: `commit` for commits, `pr` for pull and merge requests,
`release` for releases. Never hand-write `git commit`, `gh pr create`, `gh release create`,
`glab mr create`, or `glab release create` instead. Reconfiguring the workflow is `/setup`, which
only the user can run.

Read `.agents/ship.md` before changing any git workflow state.
<!-- /skrrt:ship -->
