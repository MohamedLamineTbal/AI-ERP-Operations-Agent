# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                # Python 3.12 + exact locked deps into .venv; edit pyproject.toml, then `uv lock`, to change them
cp .env.example .env                   # then set DATABASE_ADMIN_URL, DATABASE_URL, READONLY_DATABASE_URL, TEST_DATABASE_URL, GROQ_API_KEY, and ERP_ACCESS_KEY
uv run python seed.py                  # as the admin login: create tables + the erp_app and erp_readonly logins, seed sample data (skips if customers exist)
uv run uvicorn api:app --reload        # web app at http://127.0.0.1:8000 (Desk + Records)
uv run python main.py                  # interactive terminal: one question per run, y/n approval prompt
uv run pytest                          # full suite
uv run pytest tests/test_graph.py::test_name  # single test
```

There is no lint/format configuration in the repo.

Testing notes:
- `config.py` raises at import time if `DATABASE_URL` or `READONLY_DATABASE_URL` is unset, so almost every module (and test) needs a `.env`.
- Tests never touch the development database. `tests/conftest.py` repoints the URLs before anything imports `config`: `TEST_DATABASE_URL` (a login that can create tables and roles) becomes `DATABASE_ADMIN_URL`, and `DATABASE_URL` and `READONLY_DATABASE_URL` keep their logins but use the test database. It refuses to run if that is the development database, rebuilds it from the seed at the start of each run, and reseeds after any test that committed a write. So database tests can rely on fresh seed data, and don't need cleanup to protect real data. Any module that loads `config` at import time must keep working under that override.
- The app login can't delete rows or update most columns, so tests arrange data through `admin_engine` (`seed.create_admin_engine()`), and run the code under test on the app's own engines.
- Graph tests avoid Groq by injecting fake node classes into `build_erp_graph(nodes=...)` or fake LLMs into `ERPGraphNodes(llm=...)`. Follow that pattern rather than calling the real model.
- CI (GitHub Actions) installs from `uv.lock` and runs the full suite on every pull request against a throwaway PostgreSQL 16, with dummy values for every secret. No test may need a real Groq key, LangSmith, or network access beyond the database. A second job fails on any known vulnerability in the locked dependencies.
- `test_tracing_config.py` asserts that `.env.example` keeps LangSmith tracing off by default. Update it if you change that file.

## Architecture

A natural-language ERP assistant over PostgreSQL. Reads go through LLM-generated, read-only SQL. Writes go only through fixed Python business tools, and only after a human approves them.

**Entry paths:** `main.py` (terminal) and `api.py` (FastAPI, serves `web/`) both drive `graph.py` (`build_erp_graph`, `initial_state`, `run_erp_graph`, `resume_erp_graph`). Both call `check_app_login` at startup and refuse a `DATABASE_URL` login that is a superuser, can create roles or databases, or owns the ERP tables. Every `/api` route needs the `erp_session` cookie, set by `POST /api/session` with `ERP_ACCESS_KEY`; `create_app` refuses to start without a key, and `TrustedHostMiddleware` only accepts `127.0.0.1`/`localhost`. API tests open a session through `_client` in `tests/test_api.py`. `POST /api/ask` streams NDJSON events: one `step` per graph node (`STEP_LABELS`), then an `answer` or an `approval`. Pending approvals live in memory in `create_app` and are resumed by `POST /api/approvals/{id}`; they are lost when the server restarts. The API never returns internal error text to the browser; details go to the `erp_agent` log. `GET /api/records` returns every table for the Records pages, and `web/app.js` derives summaries, filters, and detail panels from it. The LangGraph `StateGraph` uses an `InMemorySaver` checkpointer. `request_approval` calls `interrupt()`, the caller reads the payload with `get_interrupt_payload`, then resumes on the same `thread_id` with `Command(resume="approved"|"rejected")`.

**Graph flow (`graph.py`):**
- `understand_request` sorts each request into one of four kinds: `query`, `action` (one directly identified record), `query_action` (select matching rows first, then act on every match), or `other` (greetings and off-topic requests, which get the fixed `OFF_TOPIC_ANSWER` and go straight to `record_outcome`). `_infer_query_action` checks with regex before the LLM classifier runs ("restock all low-stock products", "remind all overdue invoices"). Only `create_purchase_order` and `mark_invoice_reminder_sent` support `query_action`.
- SQL loop: `generate_sql` → `check_sql` (LangChain's `sql_db_query_checker` plus `validate_readonly_sql`) → `execute_sql`, with retries capped at `MAX_SQL_ATTEMPTS = 3` via `retry_count`. For `query_action`, `generate_sql` adds required output columns (e.g. `product_id`, `stock_quantity`, `reorder_threshold`). `_prepare_query_actions` then turns the rows into proposals without using the LLM. Purchase quantity = `2 * reorder_threshold - stock_quantity`. `execute_readonly_sql` returns at most `MAX_RESULT_ROWS` (1000) rows plus a `truncated` flag, using a server-side cursor. The model only sees rows that fit in `MODEL_RESULT_CHARS`, and is told when it sees part of the result: the account's Groq limit is 8,000 tokens per minute, and the result is sent twice per question. A query-then-action request whose selection was truncated is refused rather than acted on partially.
- Self-check: after `answer_query`, `verify_answer` asks the model (structured `AnswerReview`) whether the SQL answers the question and the answer matches the rows. A rejected query goes back to `generate_sql` with the reviewer's reason and uses up the same `MAX_SQL_ATTEMPTS`. A rejected answer is rewritten up to `MAX_ANSWER_ATTEMPTS` times in total. Once those limits are reached, the answer is shown with a warning. If the reviewer call itself fails, the answer is kept and `verification` is `unavailable`. Query-then-action requests skip this step, because a human reviews the slip.
- Every run ends in `record_outcome`, which logs one JSON line to the `erp_agent` logger. `main.py` writes it to `erp_agent.log`. Every fake node class in `tests/` must define each node that `build_erp_graph` registers, including `verify_answer`.
- Action path: `prepare_action` → `validate_action` → `request_approval` → `execute_action` → `finish_action`. `validate_action` is where purchase orders get priced: it looks up `products.unit_cost` and adds per-line and total costs to the approval payload. Keep DB reads out of `request_approval`, because LangGraph re-runs that node from the start when it resumes. `execute_action` re-validates and runs all approved proposals in a single `Session.begin()` transaction, so a batch succeeds or fails as a whole.
- Nodes report failures by writing `state["error"]`; they don't raise. Routing functions (`_route_*`) branch on `error`, `request_type`, `retry_count`, and `approval_status`.

**Three tool layers in `tools/` (keep them separate):**
- `sql_tools.py` is the read-only boundary. A custom tokenizer ignores comments and string literals, then rejects forbidden keywords and side-effecting functions (`FORBIDDEN_KEYWORDS`, `FORBIDDEN_FUNCTIONS`). Execution runs on `readonly_engine` (`READONLY_DATABASE_URL`), a login `seed.py` creates with `SELECT` on the six ERP tables and nothing else, inside `SET TRANSACTION READ ONLY` with a 5s `statement_timeout`, and the transaction always rolls back. That login is the real boundary; the tokenizer is a first filter. Never point it at a superuser or the `DATABASE_URL` login. All model-generated SQL must go through `validate_readonly_sql` / `execute_readonly_sql`.
- `action_tools.py` holds the real mutating functions. They run as `erp_app` (`DATABASE_URL`), which `seed.py` grants `SELECT` on the six tables, `INSERT` on `purchase_orders`, and `UPDATE` only on `orders(status, cancellation_reason)` and `invoices(reminder_sent_at)`. A new write, or a row lock (`FOR UPDATE` needs `UPDATE` on the table), needs a matching grant in `grant_logins`. They are ORM-based, with Pydantic `*Input` models (`extra="forbid"`) and `BusinessActionError` for rule violations. They accept an optional `_session` so the graph can batch them. They are never exposed to the LLM directly.
- `proposal_tools.py` holds the LLM-facing tools with the same names (`create_purchase_order`, `cancel_order`, ...). They only return a `PendingAction` JSON describing `what_will_change`. `prepare_action_proposal` binds them to the LLM, and a regex-based `_fallback_action_proposal` covers cases where the model doesn't call a tool.

Adding a new business action means updating all of: an `*Input` model and function in `action_tools.py`, any new grant it needs in `seed.py`, a proposal tool in `proposal_tools.py`, the `ActionToolName` literal, and `_execute_business_action` in `graph.py`. If the action supports set-based use, also update `_infer_query_action`, the `generate_sql` selection requirements, and `_prepare_query_actions`.

**`agent.py`** contains an older LangChain `create_sql_agent` (`ask_erp`). It is still tested but not used by `main.py`. The graph imports its shared helpers: `create_groq_model` (temperature 0) and `create_readonly_database`, a `SQLDatabase` subclass that sends `run()` through `execute_readonly_sql` and is limited to the six ERP tables in `ERP_TABLES`.

**Data:** `models.py` defines SQLAlchemy 2.0 declarative models for customers, products, orders, order_items, invoices, and purchase_orders. `products.price` is the selling price and `products.unit_cost` the supplier cost. `SALES_GUIDANCE` in `agent.py` tells both SQL prompts which is which, and that customer spending, sales, and revenue exclude cancelled orders (as the Records pages do), and `purchase_orders.unit_cost` records the cost at creation. Valid order statuses are `pending|processing|shipped|completed|cancelled`. `ORDER_TRANSITIONS` in `action_tools.py` is the only definition of which changes are allowed: one step forward at a time, and `cancelled` only from `pending` or `processing` through `cancel_order`. `/api/records` sends each order's `nextStatuses` and `cancellable` from it, so `web/app.js` never repeats the rule. New purchase orders are created as `draft`. There are no migrations: `seed.py` calls `Base.metadata.create_all`, so schema changes to existing tables require recreating the database.

**Config:** the LLM is Groq via `langchain-groq` (`GROQ_MODEL`, default `openai/gpt-oss-120b`). LangSmith tracing is optional and controlled by env vars. `config` must be imported before LangChain/LangGraph so `.env` is loaded before tracers initialize. `agent.py` and `graph.py` both do this deliberately.

<!-- skrrt:ship -->
## Architecture decisions

`docs/decisions.md` is the ADR log. When a change makes a significant architectural or technical decision (new
boundaries, providers, data flows, security controls, or anything costly to reverse), add an ADR or amend the existing
one in the same change. Supersede rather than delete, and never renumber. Base every reason on the user's words, the
code, or the project history; if no reason is recorded, leave it out and ask. Routine changes and implementation
details get no ADR.

## Git workflow

Use the Skrrt ship skills for git work: `commit` for commits, `pr` for pull and merge requests,
`release` for releases. Never hand-write `git commit`, `gh pr create`, `gh release create`,
`glab mr create`, or `glab release create` instead. Reconfiguring the workflow is `/setup`, which
only the user can run.

Read `.agents/ship.md` before changing any git workflow state.
<!-- /skrrt:ship -->
