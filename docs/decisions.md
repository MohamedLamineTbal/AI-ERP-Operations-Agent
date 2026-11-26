# Architecture Decision Records

Significant technical and architectural decisions for the ERP AI Operations Agent. Each record says what was decided,
why, what it was chosen over, and what it costs. Records are numbered in order and never renumbered. A decision that
changes is amended in place, or superseded by a new record that links back to it.

These are historical records, not justifications. Every **Rationale** names its source: a commit message, the README,
a pull request, or the security audit tracked in #28. **Not recorded** means the project history holds no reason for
the decision. Consequences are effects observed in the code; they are not the reasons the decision was made, unless a
source says so.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](#adr-0001-model-reads-through-sql-writes-only-through-fixed-business-actions) | Model reads through SQL; writes only through fixed business actions | Accepted, amended by 0002 |
| [0002](#adr-0002-the-database-not-the-validator-is-the-read-only-boundary) | The database, not the validator, is the read-only boundary | Accepted |
| [0003](#adr-0003-every-write-waits-for-explicit-human-approval) | Every write waits for explicit human approval | Accepted |
| [0004](#adr-0004-orchestrate-with-an-explicit-langgraph-state-graph) | Orchestrate with an explicit LangGraph state graph | Accepted |
| [0005](#adr-0005-bulk-changes-select-rows-with-sql-then-build-proposals-in-code) | Bulk changes select rows with SQL, then build proposals in code | Accepted |
| [0006](#adr-0006-the-model-checks-its-own-answers-before-replying) | The model checks its own answers before replying | Accepted |
| [0007](#adr-0007-costs-come-from-the-database-never-from-the-model) | Costs come from the database, never from the model | Accepted |
| [0008](#adr-0008-business-definitions-are-stated-once-and-shared-by-every-sql-prompt) | Business definitions are stated once and shared by every SQL prompt | Accepted |
| [0009](#adr-0009-groq-hosted-model-at-temperature-0) | Groq-hosted model at temperature 0 | Accepted |
| [0010](#adr-0010-web-app-served-by-the-api-with-an-access-key-session) | Web app served by the API, with an access-key session | Accepted |
| [0011](#adr-0011-schema-from-the-models-without-migrations) | Schema from the models, without migrations | Accepted |
| [0012](#adr-0012-github-flow-with-squash-merged-pull-requests) | GitHub Flow with squash-merged pull requests | Accepted |

## ADR-0001: Model reads through SQL; writes only through fixed business actions

**Status:** Accepted. Amended by [ADR-0002](#adr-0002-the-database-not-the-validator-is-the-read-only-boundary).

**Context.** Users ask questions and request changes in natural language. The model answers questions by writing SQL.

**Decision.** The model may generate SQL only for reads. Every change goes through a small, fixed set of Python
business actions: create a purchase order, cancel an order, change an order status, record an invoice reminder. Each
action validates its input strictly and enforces business rules. The model never sees the real actions; it gets
look-alike proposal tools that only describe the change.

**Rationale.** From the README: modifications are "performed through predefined Python tools rather than unrestricted
LLM-generated SQL", which "separates AI-generated data retrieval from controlled application-side mutations".

**Alternatives considered.** Letting the model generate the `UPDATE` statement itself. The README uses this as the
counter-example the design avoids, without saying more.

**Consequences.**

- Reads stay open-ended, and writes are bounded to what the code allows.
- Adding an action touches several places that must stay in step: the action, its proposal tool, the action-name type,
  and the graph's dispatcher, plus the bulk path when the action supports sets.
- Business rules are only enforced if they live in the actions. Rules left to the prompt are not (#10, #11).

## ADR-0002: The database, not the validator, is the read-only boundary

**Status:** Accepted. Amends [ADR-0001](#adr-0001-model-reads-through-sql-writes-only-through-fixed-business-actions).

**Context.** Read-only SQL was first enforced in the application: a tokenizer that rejects write keywords and a list of
side-effecting functions, then execution inside a read-only transaction with a 5-second timeout. The queries ran as the
application's own database login, which on a default local PostgreSQL is a superuser.

**Decision.** Model-generated SQL runs as a separate login, `erp_readonly`, configured on its own connection URL. It can
`SELECT` from the six ERP tables and nothing else, and has no superuser rights. The seed script creates or updates it,
and refuses to reuse the application's login. The tokenizer and the read-only transaction stay as a first filter.

**Rationale.** From the security audit (#28) and the fix's commit and PR (#29): a superuser can read server files and
password hashes inside a read-only transaction, and `query_to_xml` runs any SQL string past the function denylist, so a
question could make the agent read the `.env` file.

**Alternatives considered.** Extending the keyword and function denylist. The audit recommended a restricted login
instead, because the `query_to_xml` bypass showed the denylist could be circumvented.

**Consequences.**

- What a query can reach is decided by database permissions, so a validator bug no longer exposes the server.
- Setup needs a second connection URL and one seed run, a breaking change for existing installs.
- The running application still connects as a superuser for its own reads and writes (#17).

## ADR-0003: Every write waits for explicit human approval

**Status:** Accepted.

**Context.** The model proposes which action to run and with which arguments
([ADR-0001](#adr-0001-model-reads-through-sql-writes-only-through-fixed-business-actions)).

**Decision.** No change runs until a person approves it. The workflow pauses with a LangGraph `interrupt()` that
carries the full proposal, including what will change and its cost, and resumes on the same thread with "approved" or
"rejected". A batch of changes is approved as one slip and executes in a single transaction, so it succeeds or fails as
a whole. Rejecting leaves the database unchanged.

**Rationale.** Partly recorded. The README states the rule ("State-changing operations are never executed immediately";
"Rejecting an action leaves the database unchanged") but not why it was adopted. For batches, AGENTS.md records the
reason for one transaction: "so a batch succeeds or fails as a whole".

**Consequences.**

- The approval step must not read the database, because LangGraph re-runs that node from the start when it resumes
  (recorded in AGENTS.md). Pricing and validation happen in the step before it.
- Waiting approvals and graph state live in memory: they are lost on restart and don't work across processes (#8).
- Approval is the last line of defense, so a misleading slip becomes an approval risk (#7, #15).

## ADR-0004: Orchestrate with an explicit LangGraph state graph

**Status:** Accepted.

**Context.** LangChain's prebuilt `create_sql_agent` lets the model decide which tool to call next, in a loop. The
codebase still contains an older agent built that way. It is tested, and the graph reuses its model and database
helpers, but nothing uses it to serve requests. Both existed in the first commit.

**Decision.** Requests run through a LangGraph `StateGraph` with named steps: classify the request; generate, check and
run SQL; answer and verify; or prepare, validate, approve and execute a change. Code routes between steps by inspecting
the state; the model doesn't choose the next step. Retries are capped at three SQL attempts in total and two answer
drafts. Steps report failures by writing an error into the state instead of raising.

**Rationale.** Partly recorded. The README describes LangGraph as "stateful agent orchestration" and says retries are
limited "to prevent uncontrolled execution loops". Why a custom graph was preferred over the prebuilt agent, and why
steps report errors through state rather than exceptions, is not recorded.

**Consequences.**

- Every path through the system is visible in one place, and can be tested with fake nodes and a fake model, without a
  real model call.
- The human-approval pause ([ADR-0003](#adr-0003-every-write-waits-for-explicit-human-approval)) is a first-class step.
- Because errors are plain strings in the state, their type is lost: user-safe business refusals and internal failures
  look the same to the API (#9).
- Each extra step is an extra model call per request.

## ADR-0005: Bulk changes select rows with SQL, then build proposals in code

**Status:** Accepted.

**Context.** Requests like "restock all low-stock products" or "remind all overdue invoices" apply one action to a set
of records that must first be found.

**Decision.** These requests take a query-then-action path. The model writes the selecting SQL, which must return
specific columns, and code turns each selected row into a proposal without a model call. Restock quantity follows a
fixed rule: top the product up to twice its reorder threshold. A regex check sends the supported phrasings down this
path before the model classifier runs. Only purchase orders and invoice reminders support it.

**Rationale.** Partly recorded. The code describes the result as "deterministic proposals" built from the selected rows.
Why proposals are built in code rather than by the model, why the regex check runs before the classifier, and where
the "twice the threshold" rule comes from are not recorded.

**Consequences.**

- Quantities and proposal contents can be tested without a model.
- The set is only as correct as the model's SQL. Eligibility rules that live only in the prompt are not enforced
  (#10, #11).
- The regex check doesn't understand negation or questions (#7).
- These requests skip answer verification ([ADR-0006](#adr-0006-the-model-checks-its-own-answers-before-replying)),
  because a person reviews every row on the slip (recorded in the commit that added verification).

## ADR-0006: The model checks its own answers before replying

**Status:** Accepted.

**Context.** A query that ran cleanly could still answer the wrong question, and nothing checked that the final answer
matched the result rows.

**Decision.** After drafting an answer, a review step asks the model, with structured output, whether the SQL answers
the question and whether every fact in the answer is supported by the rows. A rejected query is regenerated with the
reviewer's reason, sharing the three-attempt SQL limit. A rejected answer is rewritten once. When the limits run out,
the answer is shown with a warning. If the review call itself fails, the answer is kept and marked as unchecked.

**Rationale.** From the commit that introduced it: the context above, plus that a wrong answer should be "shown with a
warning instead of passing as correct".

**Consequences.**

- Wrong answers are flagged, and the web app shows which answers were checked.
- One to three extra model calls per question, and the full result is sent to the model again (#3, #4).
- The reviewer is the same model as the generator, so it can share the generator's blind spots. This is a check, not a
  guarantee.

## ADR-0007: Costs come from the database, never from the model

**Status:** Accepted.

**Context.** Approvers could not see what a purchase order would cost, because products only stored a selling price.

**Decision.** Products carry a supplier unit cost alongside their selling price. When a purchase order is proposed, the
validation step looks up the current unit cost and adds a cost per line and a batch total to the approval slip. Each
purchase order stores the unit cost at the moment it is created. The model never supplies a cost.

**Rationale.** From the commit that introduced it: the context above. The commit states that "the model never supplies
a cost" as a property of the design, without saying why.

**Consequences.**

- Approvers see a real cost, and past purchase orders keep the price they were placed at.
- The cost is computed when the slip is created. A slip approved later executes with the old numbers (#8).
- It added two required columns, so existing databases had to be recreated
  ([ADR-0011](#adr-0011-schema-from-the-models-without-migrations)).

## ADR-0008: Business definitions are stated once and shared by every SQL prompt

**Status:** Accepted.

**Context.** Adding a supplier cost next to the selling price created two price columns the model could confuse in
revenue questions. Separately, with nothing saying how to treat cancelled orders, the model summed them into customer
spending, so its totals disagreed with the Records pages: asking which customer had spent a figure shown on the Records
page found nobody.

**Decision.** A single block of business definitions is included in every prompt that writes or reviews SQL: selling
price versus supplier cost, and spending, sales and revenue excluding cancelled orders. New definitions go into the
same block.

**Rationale.** From the two commits that added the definitions: the price guidance was added so "revenue questions keep
using price", and the cancelled-order rule so the agent's totals match the Records pages.

**Consequences.**

- One definition per business term, shared by generation and review, so the two steps can't disagree.
- The frontend computes the same figures separately, so each definition must be kept in step by hand. "Low stock"
  already differs between the two (#12).
- The definitions guide the model, but nothing enforces them.

## ADR-0009: Groq-hosted model at temperature 0

**Status:** Accepted.

**Decision.** All model calls go to Groq through `langchain-groq`, with `openai/gpt-oss-120b` as the default model,
configurable by environment variable, at temperature 0 with two retries.

**Rationale.** Not recorded: neither the choice of provider and model, nor the temperature.

**Consequences.**

- Structured output on this provider is a forced tool call. Some phrasings make the model answer in prose instead, and
  Groq rejects the response (#14).
- Query results and the schema text sent with every question, including sample rows, go to a third party (#3).

## ADR-0010: Web app served by the API, with an access-key session

**Status:** Accepted.

**Context.** The agent was first usable only from the terminal. Once it had a web API, any program on the machine, or a
web page that rebinds its domain to `127.0.0.1`, could read every record and approve changes, because the API had no
authentication and accepted any Host header.

**Decision.** A FastAPI server serves the web app from the same origin as its API: static HTML, CSS and JavaScript with
no build step. Answers stream as newline-delimited JSON, with progress events as the graph runs. Every API route
requires a session cookie, opened by posting a shared access key from `.env`. The cookie is `HttpOnly` and
`SameSite=Strict`, and holds a hash of the key, not the key. The server refuses to start without a key, and answers
only requests addressed to `127.0.0.1` or `localhost`. The browser never receives internal error text.

**Rationale.** Partly recorded. The web app's commit gives the reason for having one: the agent "was only usable from
the terminal". The access-key session and the host check come from the security audit (#28) and its fix (#29), to stop
the access described above. Why the frontend has no framework or build step is not recorded. The audit called for "a
login or token"; choosing one shared key over per-user accounts was made while implementing the fix, and no further
reason is recorded.

**Consequences.**

- One process serves everything, and being same-origin keeps the cookie and cross-site protection simple.
- There is one shared key and no user identity, so approvals can't be attributed to a person.
- Serving beyond the local machine needs deliberate changes to the host allowlist and to how users sign in.

## ADR-0011: Schema from the models, without migrations

**Status:** Accepted.

**Decision.** The seed script creates the database schema directly from the SQLAlchemy models. There is no migration
tool.

**Rationale.** Not recorded.

**Consequences.**

- Setup is one command.
- Any change to an existing table means recreating the database. Purchase-order costing
  ([ADR-0007](#adr-0007-costs-come-from-the-database-never-from-the-model)) already required it, and schema-level
  consistency work (#25) would too.
- Data could not be upgraded in place. Adopting a migration tool would be the first step toward that.

## ADR-0012: GitHub Flow with squash-merged pull requests

**Status:** Accepted.

**Context.** The repository has one long-lived branch and no release branches. When this was decided, it had no CI
either.

**Decision.** `main` is the only long-lived branch and must stay deployable. Every change arrives through a short-lived
branch and a pull request into `main`, squash-merged once required checks pass. No commits go directly to `main`. The
rules live in the ship policy file that the commit, PR and release workflows read.

**Rationale.** From the commit that adopted it: with only `main`, no release branches and no CI, "GitHub Flow fits".
The project owner later confirmed pull requests over direct pushes to `main`.

**Consequences.**

- Each change is meant to land on `main` as one reviewable commit.
- "Required checks" means nothing until CI exists (#27), and `main` is not protected on GitHub, so the policy is followed
  by convention, not enforced. Pull requests #1, #2 and #29 merged as merge commits rather than squashes.
