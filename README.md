# 🐕 Walking Hounds

Multi-agent dog-walking business automation. AI agents handle email intake,
scheduling, communication, invoicing, and reminders — the human only steps in
for approvals, complaints, and payment validation via the dashboard.

Built for a dear friend: a passionate dog lover who puts animal welfare first.
The goal is to run the business on autopilot — happy dogs, perfect walking
groups, minimal stress — so she can focus on the dogs, not the admin.

## Architecture

```
Email → IntakeAgent → OnboardingAgent → Human Approval → SchedulingAgent
                                       ↓
                          CommunicationAgent → InvoicingAgent
                               ↑                    ↑
                          ReminderAgent        LoggerAgent → Dashboard (human)
```

### Event-Driven Core

All agents communicate exclusively through typed events on an async `EventRouter`
(pub/sub backed by SQLite). No direct calls between agents.

### Agent Roster

| Agent | Role | Subscribes To |
|---|---|---|
| **IntakeAgent** | Polls IMAP, parses emails with LLM, classifies intent, detects onboarding replies | *(poll-driven)* |
| **OnboardingAgent** | Sends welcome email, collects dog info, creates pending records, routes to human approval | `OnboardingStarted, DogInfoProvided, HumanApproved` |
| **SchedulingAgent** | Books walks, assigns walkers, manages groups (puppy, in-heat, intact male separation) | `BookingIntent, CancellationIntent, RescheduleIntent, HumanApproved` |
| **CommunicationAgent** | Sends all outbound emails, LLM-drafted responses | `ScheduleConfirmed, CancellationConfirmed, ClarificationRequest, QueryIntent, ComplaintIntent, ReminderDue, PaymentReminder` |
| **InvoicingAgent** | Creates invoices, tracks payments, escalates overdue (7-day reminder, 14-day human escalation) | `ScheduleConfirmed, CancellationConfirmed, WalkCompleted, PaymentConfirmed` |
| **ReminderAgent** | Walk reminders (2h before), walker morning briefings (08:00), post-walk feedback requests | *(timer-driven)* |
| **LoggerAgent** | Audit trail — records every event to journal, persists approval gates | `* (wildcard)` |

### ReminderAgent — Implemented Lifecycle

The ReminderAgent runs a timer loop polling every 60 seconds. The following
triggers are **implemented and tested** (9 tests, all passing):

| Trigger | Status | Description |
|---|---|---|
| Walk reminders | ✅ Implemented | Sends `ReminderDue(walk_reminder)` 2 hours before each scheduled walk. Dedup via messages table. |
| Walk completion | ✅ Implemented | Marks walks as `completed` after slot time passes, emits `WalkCompleted` + `ReminderDue(feedback)`. |
| Walker morning briefings | ✅ Implemented | Sends `ReminderDue(walker_briefing)` at 08:00 on business days for walkers with scheduled walks. |

**Not yet implemented** (tracked as future work):

- Invoice overdue trigger — `InvoicingAgent.check_overdue_invoices()` exists and works, but the ReminderAgent timer hook is a stub (`pass`)
- Next-day schedule confirmation (20:00 reminder to clients with walks tomorrow)
- LoggerAgent daily summary journal entry at 22:00 (currently reactive only)

### Human Gates (never automated)

- **New-client onboarding** — Dog info collected via email → human approves before activation
- **Complaints** — LLM drafts a response, human approves before sending
- **Payments** — Human marks invoices as paid via dashboard
- **Schedule conflicts** — When no walker/slot is available
- **Payment escalation** — 14+ days overdue invoices

### Onboarding Flow

When an unknown client emails, the system doesn't just block them — it automates
registration with human-in-the-loop:

1. **OnboardingStarted** → OnboardingAgent sends a structured welcome email asking for dog details (name, breed, age, sex, etc.)
2. Client replies with info → IntakeAgent detects the onboarding session → emits **DogInfoProvided**
3. OnboardingAgent validates required fields, creates pending client+dogs records → emits **HumanApprovalRequired**
4. Human reviews in dashboard → **Approve & Activate** or Reject
5. On approval: client activated, ready to book walks

Rate limiting: configurable via `ONBOARDING_RATE_LIMIT_PER_MIN=0` in `.env` (0 = disabled for testing).

## Business Rules

- **Schedule**: Mon–Fri, staggered lunchtime walks (11:30, 12:00, 12:30)
- **Groups**: Max 4 dogs per group, max 3 groups per day
- **Puppies** (4–10 months): Kept in separate puppy groups, never mixed with adults
- **In-heat females**: Only grouped with females + neutered males (excluded from intact males)
- **Intact males**: Only grouped with males + spayed females (excluded from in-heat females)
- **No back-to-back**: 45-min walk + 15-min break. Walker at 11:30 blocked from 12:00, free at 12:30
- **Pricing**: €20/walk, invoiced immediately at booking confirmation
- **Cancellation**: >24h = full refund, <24h = 50% charge

## Tech Stack

- **Python 3.13**, asyncio
- **SQLite** (via aiosqlite) — business data + event store
- **FastAPI + Jinja2 + HTMX** — dashboard with dark theme, WebSocket live updates
- **Ollama** (llama3.1:8b) — local LLM for email parsing and query drafting
- **IMAP/SMTP** (aioimaplib/aiosmtplib) — email integration
- **Pydantic** — typed events and settings
- **pytest + pytest-asyncio** — 233 tests

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
IMAP_FOLDER=walking-hounds
SMTP_HOST=smtp.gmail.com
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
DASHBOARD_PORT=8010
INTAKE_DEMO_MODE=false
ONBOARDING_RATE_LIMIT_PER_MIN=0
```

> **Note:** Gmail requires an App Password (not account password). Enable 2-Step
> Verification, then generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

## Dashboard Pages

| Route | Description |
|---|---|
| `/` | Today's schedule, stats, pending approvals/invoices, activity feed |
| `/schedule` | Week grid (Mon–Fri × slots) with dog badges (🐕 puppy, 🔥 in-heat, ♂️ intact) |
| `/journal` | Full audit trail — every event with expandable details |
| `/invoices` | Pending/paid invoices with "Mark Paid" button |
| `/approvals` | Human approval gates with one-click resolve (onboarding, complaints, conflicts) |
| `/agents` | System health, event store stats, DLQ count, agent roster |
| `/ws` | WebSocket for live updates |

## Testing

```bash
# Full suite (233 tests)
pytest

# Just the reminder agent tests
pytest tests/test_reminder_agent.py -v

# Just the e2e integration tests
pytest tests/test_e2e_*.py -v
```

## Project Structure

```
src/
├── agents/          # 7 agents (intake, onboarding, scheduling, communication, invoicing, reminder, logger)
├── router/          # EventRouter (pub/sub), EventStore (SQLite), 20+ typed events
├── db/              # Database schema, seed data (12 clients, 3 walkers, 14 dogs)
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
