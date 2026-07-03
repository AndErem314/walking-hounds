# 🐕 Walking Hounds

Multi-agent dog-walking business automation. AI agents handle email intake,
scheduling, communication, invoicing, and reminders — the human only steps in
for approvals, complaints, and payment validation via the dashboard.

## Architecture

```
Email → IntakeAgent → SchedulingAgent → CommunicationAgent → InvoicingAgent
                         ↑                    ↑                    ↑
                    ReminderAgent        LoggerAgent          Dashboard (human)
```

### Event-Driven Core

All agents communicate exclusively through typed events on an async `EventRouter`
(pub/sub backed by SQLite). No direct calls between agents.

### Agent Roster

| Agent | Role | Subscribes To |
|---|---|---|
| **IntakeAgent** | Polls IMAP, parses emails with LLM, classifies intent | *(poll-driven)* |
| **SchedulingAgent** | Books walks, assigns walkers, manages groups | `BookingIntent, CancellationIntent, RescheduleIntent, HumanApproved` |
| **CommunicationAgent** | Sends all outbound emails, LLM-drafted responses | `ScheduleConfirmed, CancellationConfirmed, ClarificationRequest, QueryIntent, ComplaintIntent, ReminderDue, PaymentReminder` |
| **InvoicingAgent** | Creates invoices, tracks payments, escalates | `ScheduleConfirmed, CancellationConfirmed, WalkCompleted, PaymentConfirmed` |
| **ReminderAgent** | Walk reminders, walker briefings, feedback requests | *(timer-driven)* |
| **LoggerAgent** | Audit trail — records every event to journal | `* (wildcard)` |

### Human Gates (never automated)

- **Complaints** — LLM drafts a response, human approves before sending
- **Payments** — Human marks invoices as paid via dashboard
- **New clients** — Unknown email senders require approval
- **Schedule conflicts** — When no walker/slot is available
- **Payment escalation** — 14+ days overdue invoices

## Business Rules

- **Schedule**: Mon–Fri, staggered lunchtime walks (11:30, 12:00, 12:30)
- **Groups**: Max 4 dogs per group, max 3 groups per day
- **Puppies** (4–10 months): Kept in separate puppy groups, never mixed with adults
- **Pricing**: €20/walk, invoiced immediately at booking confirmation
- **Cancellation**: >24h = full refund, <24h = 50% charge

## Tech Stack

- **Python 3.11+**, asyncio
- **SQLite** (via aiosqlite) — business data + event store
- **FastAPI + Jinja2 + HTMX** — dashboard with dark theme
- **Ollama** (llama3.1:8b) — local LLM for email parsing and query drafting
- **IMAP/SMTP** (aiosmtplib/aioimaplib) — email integration
- **Pydantic** — typed events and settings
- **pytest + pytest-asyncio** — testing

## Quick Start

```bash
# Create and activate a virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Initialize database + seed data
walking-hounds init-db
walking-hounds seed

# Start everything (agents + dashboard on http://localhost:8010)
walking-hounds start
```

### Configuration

Create a `.env` file (see `.env.example`):

```env
IMAP_HOST=imap.gmail.com
IMAP_USER=your-email@gmail.com
IMAP_PASSWORD=your-app-password
SMTP_HOST=smtp.gmail.com
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
DASHBOARD_PORT=8010
```

## Dashboard Pages

| Route | Description |
|---|---|
| `/` | Today's schedule, stats, pending approvals/invoices, activity feed |
| `/schedule` | Walks grouped by slot with dog details |
| `/journal` | Full audit trail — every event with expandable details |
| `/invoices` | Pending/paid invoices with "Mark Paid" button |
| `/approvals` | Human approval gates with one-click resolve |
| `/agents` | System health, event store stats, agent roster |
| `/ws` | WebSocket for live updates |

## Testing

```bash
# Full suite (216 tests)
pytest

# Just the e2e integration tests
pytest tests/test_e2e_*.py -v
```

## Project Structure

```
src/
├── agents/          # 6 agents (intake, scheduling, communication, invoicing, reminder, logger)
├── router/          # EventRouter (pub/sub), EventStore (SQLite), typed events
├── db/              # Database schema, seed data
├── email/           # IMAP client, SMTP client, templates
├── llm/             # Ollama client
├── dashboard/       # FastAPI app, Jinja2 templates, CSS
├── config.py        # Pydantic settings (.env)
├── cli.py           # Click CLI (start, init-db, seed, status, dlq)
└── main.py          # Entry point — boots everything
tests/
├── test_e2e_*.py    # End-to-end integration tests
├── test_*.py        # Unit tests per component
```

## License

MIT
