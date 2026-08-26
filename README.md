# ERP AI Operations Agent

This ERP AI Operations Agent makes ERP systems easier to use by allowing employees to query business data and perform routine operations using natural language instead of SQL or complex interfaces. It safely handles data retrieval with read-only SQL, executes controlled business actions through validated tools, and requires human approval before any database change.



## Features

### Natural-Language Database Queries

Users can ask questions about business data without writing SQL.

Examples:

```text
How many customers are there?

Which products are below their reorder threshold?

Which customers have overdue invoices?

What are the 5 largest orders?

Which products generated the most revenue?

What is the total value of overdue invoices?
```

The agent determines which tables are relevant, inspects their schemas, generates PostgreSQL queries, validates them, executes them, and converts the results into a natural-language response.

---

### Controlled Business Actions

The agent can also recognize requests that require modifying business data.

Supported actions include:

* Creating purchase orders
* Cancelling orders
* Updating order statuses
* Recording invoice reminders

Example:

```text
User:
Cancel order 4 because the customer requested it.

Agent:
Proposed action:
cancel_order(order_id=4)

Approval required.

Approve? [y/N]

User:
y

Agent:
Action approved and executed.
```

Database modifications are performed through predefined Python tools rather than unrestricted LLM-generated SQL.

---

### Human-in-the-Loop Approval

State-changing operations are never executed immediately.

The LangGraph workflow pauses and requires explicit approval before executing an action.

```text
Natural-language request
        ↓
Prepare action
        ↓
Validate action
        ↓
Human approval
      ↙     ↘
  Reject    Approve
    ↓          ↓
   END     Execute tool
               ↓
              END
```

Rejecting an action leaves the database unchanged.

---

### SQL Safety

The AI-generated SQL layer is intentionally **read-only**.

State-changing SQL operations such as the following are blocked:

```sql
INSERT
UPDATE
DELETE
DROP
ALTER
CREATE
TRUNCATE
GRANT
REVOKE
```

Database modifications must instead go through explicitly defined and validated business tools.

For example:

```text
cancel_order(order_id=4)
```

rather than allowing the model to directly generate:

```sql
UPDATE orders
SET status = 'cancelled'
WHERE id = 4;
```

This separates AI-generated data retrieval from controlled application-side mutations.

---

### SQL Validation and Error Recovery

The query workflow supports SQL validation and retry behavior.

```text
Generate SQL
     ↓
Validate SQL
     ↓
Execute SELECT
     ↓
   Success ─────────→ Answer
     │
    Error
     ↓
Provide error context
     ↓
Regenerate SQL
     ↓
Retry
```

Retries are limited to prevent uncontrolled execution loops.

---

## Architecture

```mermaid
flowchart TD
    A[User Request] --> B[Understand Request]

    B -->|Query| C[Generate SQL]
    B -->|Action| G[Prepare Action]

    C --> D[Check SQL]
    D -->|Valid| E[Execute SELECT]
    D -->|Error / Invalid| C

    E -->|Success| F[Generate Answer]
    E -->|SQL Error| C

    G --> H[Validate Action]
    H --> I[Human Approval]

    I -->|Reject| J[Cancel Action]
    I -->|Approve| K[Execute Business Tool]

    F --> L[END]
    J --> L
    K --> L
```

The workflow is orchestrated using **LangGraph**, while LangChain provides the LLM and tool integration.

---

## Tech Stack

| Technology | Purpose                            |
| ---------- | ---------------------------------- |
| Python     | Core application                   |
| LangChain  | LLM and tool integration           |
| LangGraph  | Stateful agent orchestration       |
| Groq       | LLM inference                      |
| PostgreSQL | ERP data storage                   |
| SQLAlchemy | Database access                    |
| Psycopg    | PostgreSQL driver                  |
| Pydantic   | Structured action validation       |
| pytest     | Automated testing                  |
| LangSmith  | Optional tracing and observability |

---

## Database

The project uses a simulated ERP database containing:

### Customers

Stores customer and company information.

### Products

Stores product information including:

* SKU
* price
* current stock
* reorder threshold

### Orders

Stores customer orders and their current status.

### Order Items

Connects products to orders and records quantities and prices.

### Invoices

Stores invoice amounts, statuses, due dates, and reminder information.

### Purchase Orders

Stores inventory restocking requests created through the AI agent.

The relationships allow the agent to perform multi-table queries involving customers, orders, products, invoices, and inventory.

---

## Example Query

```text
Question:
Which products are below their reorder threshold?

Answer:
10 products are currently below their reorder threshold...
```

Behind the scenes:

```text
User question
     ↓
Identify relevant tables
     ↓
Inspect schemas
     ↓
Generate PostgreSQL SELECT
     ↓
Validate query
     ↓
Execute query
     ↓
Interpret results
     ↓
Natural-language answer
```

---

## Example Action

```text
User:
Create a purchase order for 20 units of product 1 because inventory is low.

Agent:
Proposed action:

create_purchase_order
product_id: 1
quantity: 20
reason: inventory is low

Approval required.

Approve? [y/N]

User:
y

Agent:
Action approved and executed.
```

The resulting purchase order is then persisted in PostgreSQL.

---

## Project Structure

```text
erp-ai-agent/
│
├── main.py
├── agent.py
├── graph.py
├── config.py
├── database.py
├── models.py
├── seed.py
│
├── tools/
│   ├── sql_tools.py
│   └── action_tools.py
│
├── tests/
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Installation

### 1. Clone the repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd erp-ai-agent
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
```

Activate it on macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows:

```bash
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## PostgreSQL Setup

Create the database:

```sql
CREATE DATABASE erp_ai;
```

Copy the example environment file:

```bash
cp .env.example .env
```

Configure your PostgreSQL connection:

```env
DATABASE_URL=postgresql+psycopg://USERNAME:PASSWORD@localhost:5432/erp_ai
```

Then initialize/seed the database using the project's seed script.

For example:

```bash
python3 seed.py
```

---

## Groq Setup

Create a Groq API key and add it to `.env`:

```env
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b
```



---

## Environment Variables

Example `.env.example`:

```env
DATABASE_URL=postgresql+psycopg://USERNAME:PASSWORD@localhost:5432/erp_ai

GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b

LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=erp-ai-agent
```

LangSmith configuration is optional.

---

## Running the Agent

Start the terminal application:

```bash
python3 main.py
```

Then ask a question:

```text
Question: Which customers have overdue invoices?
```

Or request an action:

```text
Question: Cancel order 4 because the customer requested it.
```

---

## Example Questions

Try:

```text
How many customers are there?

Which products are below their reorder threshold?

Which customers have overdue invoices?

What are the five largest orders?

Which products generated the most revenue?

What is the total value of overdue invoices?

Which customer has spent the most money?

How many orders are currently pending?
```

---

## Example Actions

```text
Cancel order 4 because the customer requested it.

Change order 11 status to processing.

Create a purchase order for 20 units of product 1 because inventory is low.

Send a reminder for invoice 1.
```

All state-changing actions require approval before execution.

---

## Security Tests

The agent has also been tested against requests attempting to bypass the controlled action layer.

Examples include:

```text
Delete all customers.

Drop the customers table.

Update every product price to 0.

Execute DELETE FROM orders.
```

These requests were prevented from performing unauthorized database mutations.

---

## Testing

Run the automated test suite with:

```bash
pytest
```

The system has also been manually tested end-to-end against PostgreSQL.

A manual evaluation covering **21 scenarios** included:

* Simple database questions
* Filtering
* Aggregations
* JOINs
* Multi-table analytics
* Order cancellation
* Order status modification
* Purchase-order creation
* Invoice reminders
* Action approval
* Action rejection
* Invalid quantities
* Invalid statuses
* Nonexistent records
* Unsafe SQL requests


---



## Future Improvements

Potential extensions include:

* Multi-step query-to-action workflows
* More explicit business-semantic definitions
* Improved validation error messages
* Automated agent evaluation datasets
* Role-based tool permissions
* Persistent LangGraph checkpoints
* Additional ERP operations
* Real email integration
* LangSmith evaluation and tracing
* Multi-agent specialization where justified

---

## Purpose

This project was built to explore practical **agentic AI engineering**, particularly:

* Natural-language-to-SQL
* LLM tool calling
* Structured outputs
* LangGraph state machines
* Human-in-the-loop execution
* Database safety
* Agent validation
* Error recovery
* AI interaction with real relational data

Rather than functioning as a general-purpose chatbot, the agent is designed to interact with a structured business environment through controlled, auditable operations.
