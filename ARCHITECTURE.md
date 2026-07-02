# Walking Hounds — Multi-Agent Architecture & Implementation Plan

> **Goal:** A dog-walking business (12 clients, 3 walkers) that runs on autopilot.
> AI agents handle the operational workflow; humans step in only where genuinely needed.
> Built on an **async event-driven protocol** for stability and scalability.

---

## 1. Design Philosophy

### Core Principles
- **Event-driven, not request-driven.** Every state change is an event on an async bus. Agents subscribe to what they care about. This decouples agents, enables parallel processing, and makes the system resilient to individual agent failures.
- **Each agent owns a bounded domain.** No agent knows about the whole system. Agents communicate exclusively through typed events on the bus.
- **Human-in-the-loop gates are first-class.** Certain transitions require human approval. The system pauses that workflow branch (not the whole system) until a human acts.
- **Durability by default.** Events are persisted to SQLite before processing. If the system crashes, it resumes from the last checkpoint.
- **Async from the ground up.** `asyncio` event loop, async I/O for email/HTTP/LLM, bounded queues with backpressure.

### What Never Gets Automated (and Why)

| Activity | Reason |
|---|---|
| Physical dog walking | Dogs are living animals — safety requires a human present |
| New client onboarding (first contact) | Need human judgment for vetting, temperament assessment, contract signing |
| Payment refunds / dispute resolution | Financial liability — human must authorize |
| Emergency response (injury, lost dog) | Time-critical, high-stakes — human judgment irreplaceable |
| Price changes / negotiations | Business strategy decision |
| Walker hiring / firing / scheduling disputes | HR decision |
| Responses to legal threats or formal complaints | Legal risk — human must handle |
| Medication administration / special-needs care instructions | Health/safety liability |

### Where Humans Approve (Gated Transitions)

| Gate | Trigger | What Human Sees |
|---|---|---|
| New client onboarding | Intake Agent detects unknown client | Client details, dog info, requested service — approve or reject |
| Ambiguous intent | Intake Agent confidence < threshold | Original message + agent's best guess — human corrects |
| Schedule conflict (unresolvable) | Scheduling Agent can't auto-resolve | Proposed alternatives — human picks |
| Complaint / negative sentiment | Communication Agent detects negative tone | Drafted response — human edits and approves |
| Unusual request | Request outside known service types | Request details — human decides and drafts response |
| Failed payment (escalation) | 2nd reminder unpaid | Client payment history — human decides next step |
| Walker reassignment (manual override) | Human-initiated | Current schedule — human reassigns |

---

## 2. Agent Roster — What Each Agent Owns

```
┌─────────────────────────────────────────────────────────────────┐
│                    ASYNC EVENT BUS (pub/sub)                     │
│          SQLite-backed durable queue + asyncio tasks             │
└──────┬──────┬──────┬──────┬──────┬──────┬──────┬───────┬────────┘
       │      │      │      │      │      │      │       │
       ▼      ▼      ▼      ▼      ▼      ▼      ▼       ▼
  ┌─────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌───────┐
  │INTAKE│ │SCHED │ │COMMS │ │INVOIC│ │REMIND│ │LOGGER│ │DASH   │
  │AGENT │ │AGENT │ │AGENT │ │AGENT │ │AGENT │ │AGENT │ │AGENT  │
  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └───────┘
```

### Agent 1: Intake Agent
**Owns:** Inbound message parsing and intent classification
**Subscribes to:** `EmailReceived`, `MessageReceived`
**Emits:** `BookingIntent`, `CancellationIntent`, `RescheduleIntent`, `QueryIntent`, `ComplaintIntent`, `HumanApprovalRequired`
**Responsibilities:**
- Poll IMAP inbox (async, every 60s)
- Parse email body with LLM to extract: client name, dog name(s), date/time, service type, intent
- Classify intent into known categories
- If client unknown → emit `HumanApprovalRequired` (new client gate)
- If confidence < 0.75 → emit `HumanApprovalRequired` (ambiguous gate)
- Deduplicate (don't process same email twice — track Message-ID)

### Agent 2: Scheduling Agent
**Owns:** The schedule, walker assignments, group composition
**Subscribes to:** `BookingIntent`, `CancellationIntent`, `RescheduleIntent`, `HumanApproved`
**Emits:** `ScheduleConfirmed`, `ScheduleConflict`, `ScheduleUpdated`, `CancellationConfirmed`
**Responsibilities:**
- Maintain calendar (SQLite): walks, walker assignments, dog groups
- Auto-assign walker based on: geographic zone, dog compatibility, walker capacity
- Detect conflicts (double-booking, over-capacity, walker unavailable)
- Auto-resolve simple conflicts (e.g., swap walker with available capacity)
- Escalate unresolvable conflicts to human
- Handle cancellations: remove from schedule, notify if late cancellation (fee?)

### Agent 3: Communication Agent
**Owns:** All outbound messages (confirmations, replies, reminders, follow-ups)
**Subscribes to:** `ScheduleConfirmed`, `CancellationConfirmed`, `QueryIntent`, `ComplaintIntent`, `ReminderDue`, `HumanApprovedResponse`
**Emits:** `MessageSent`, `ConfirmationSent`, `HumanApprovalRequired`
**Responsibilities:**
- Draft and send booking confirmations (email)
- Draft and send cancellation confirmations
- Reply to general queries (LLM-drafted, template-guided)
- Detect negative sentiment → route to human approval gate
- Maintain message history per client
- Never sends without required approval for gated categories

### Agent 4: Invoicing Agent
**Owns:** Billing cycle, invoice generation, payment tracking
**Subscribes to:** `ScheduleConfirmed`, `WalkCompleted`, `PaymentReceived`, `InvoiceOverdue`
**Emits:** `InvoiceGenerated`, `PaymentReminder`, `PaymentFailed`, `HumanApprovalRequired`
**Responsibilities:**
- Generate invoices (per walk, weekly, or monthly — configurable)
- Track payment status per invoice
- Send automated payment reminders (1st: polite, 2nd: firm)
- Escalate to human after 2nd reminder unpaid
- Never processes refunds or adjusts prices (human only)

### Agent 5: Reminder Agent
**Owns:** Time-based triggers and notifications
**Subscribes to:** `ScheduleConfirmed`, `WalkStartingSoon` (timer-triggered)
**Emits:** `ReminderDue`, `WalkStartingSoon`, `WalkCompleted` (timer-based)
**Responsibilities:**
- Send walk reminders to clients (e.g., 2h before walk)
- Send walker schedule notifications (e.g., morning briefing with day's walks)
- Trigger invoicing cycles (weekly/monthly)
- Trigger follow-up messages (e.g., "How was today's walk?" feedback request)
- All timing is async — uses `asyncio.sleep` with persistence (survives restart)

### Agent 6: Logger Agent (Journal)
**Owns:** The activity journal / audit log
**Subscribes to:** ALL events (observer pattern)
**Emits:** (none — terminal sink)
**Responsibilities:**
- Record every event to the journal with timestamp
- Track: email received, confirmation sent, booking created, cancelled, invoice generated, payment received, reminder sent
- Provide queryable history for dashboard
- This is the system's memory — nothing happens without being logged

### Agent 7: Dashboard Agent
**Owns:** The web dashboard (FastAPI) and API
**Subscribes to:** `ScheduleUpdated`, `HumanApprovalRequired`, `InvoiceGenerated`, `JournalEntry` (reads from Logger)
**Emits:** `HumanApproved`, `HumanRejected`, `HumanOverride` (from dashboard actions)
**Responsibilities:**
- Serve web UI showing: today's schedule, walker assignments, pending approvals, invoice status, activity journal
- REST API for human actions: approve/reject pending items, manual schedule override, view journal
- WebSocket for real-time updates (schedule changes, new emails, approvals needed)
- This is the ONLY interface for human interaction with the system

---

## 3. Event Catalog

All events are typed Pydantic models flowing through the async bus.

| Event | Emitted By | Payload | Consumed By |
|---|---|---|---|
| `EmailReceived` | Intake Agent | message_id, from, subject, body, timestamp | (internal trigger) |
| `BookingIntent` | Intake Agent | client_name, dog_name, date, time, service_type, confidence | Scheduling Agent |
| `CancellationIntent` | Intake Agent | client_name, booking_ref, reason | Scheduling Agent |
| `RescheduleIntent` | Intake Agent | client_name, booking_ref, new_date, new_time | Scheduling Agent |
| `QueryIntent` | Intake Agent | client_name, query_text, suggested_response | Communication Agent |
| `ComplaintIntent` | Intake Agent | client_name, complaint_text, severity | Communication Agent (→ human gate) |
| `ScheduleConfirmed` | Scheduling Agent | booking_id, client, dog, walker, date, time, group | Communication, Invoicing, Logger |
| `ScheduleConflict` | Scheduling Agent | conflict_details, alternatives[] | Dashboard (→ human gate) |
| `CancellationConfirmed` | Scheduling Agent | booking_id, client, refund_due | Communication, Invoicing, Logger |
| `ConfirmationSent` | Communication Agent | to, message_type, content | Logger |
| `MessageSent` | Communication Agent | to, message_type, content | Logger |
| `InvoiceGenerated` | Invoicing Agent | invoice_id, client, amount, due_date | Communication, Logger |
| `PaymentReminder` | Invoicing Agent | invoice_id, client, reminder_count | Communication Agent |
| `ReminderDue` | Reminder Agent | reminder_type, target, booking_id | Communication Agent |
| `HumanApprovalRequired` | Various | gate_type, context, options[] | Dashboard Agent |
| `HumanApproved` | Dashboard Agent | gate_id, decision, notes | Originating agent |
| `HumanRejected` | Dashboard Agent | gate_id, reason | Originating agent |
| `WalkCompleted` | Reminder Agent (or human) | booking_id, walker, duration, notes | Invoicing, Logger |
| `JournalEntry` | Logger Agent | event_type, timestamp, details | (terminal — Dashboard reads DB) |

---

## 4. Async Protocol Design

### Event Bus Architecture

```
                    ┌──────────────────────────┐
                    │    EventBus (asyncio)     │
                    │                          │
                    │  ┌────────────────────┐  │
                    │  │  In-Memory Queue   │  │  ← Fast path: asyncio.Queue per subscriber
                    │  │  (bounded, per     │  │     Backpressure: await queue.put()
                    │  │   event type)      │  │     When full: agent slows down
                    │  └────────────────────┘  │
                    │                          │
                    │  ┌────────────────────┐  │
                    │  │  SQLite Event Store │  │  ← Durability: events persisted before dispatch
                    │  │  (WAL mode)         │  │     Recovery: replay unprocessed events on restart
                    │  └────────────────────┘  │
                    │                          │
                    │  ┌────────────────────┐  │
                    │  │  Dead Letter Queue  │  │  ← Failed events after N retries
                    │  │  (SQLite table)     │  │     Dashboard shows DLQ items for human review
                    │  └────────────────────┘  │
                    └──────────────────────────┘
```

### Key Async Patterns

1. **Publish/Subscribe with typed events**
   - Each event type has an `asyncio.Queue` (bounded, configurable per agent)
   - Publishers `await bus.publish(event)` — non-blocking if queue not full
   - Subscribers `await bus.subscribe(EventType, handler)` — each handler runs as `asyncio.Task`
   - Backpressure: if a subscriber's queue is full, publisher awaits (natural flow control)

2. **Durable event sourcing**
   - Every published event is written to SQLite (`event_store` table) BEFORE dispatch
   - Each event has: `id, type, payload (JSON), status (pending/processing/done/failed), retries, created_at, processed_at`
   - On restart: replay all `pending` and `processing` events (with idempotency keys)

3. **Agent lifecycle**
   - Each agent is an `asyncio.Task` with a main loop: `async def run(self): while True: event = await self.inbox.get(); await self.handle(event)`
   - Agents have `start()`, `stop()`, `health()` methods
   - `health()` returns status for dashboard: `{agent_name, status, queue_depth, last_processed, error_count}`

4. **Time-based triggers (cron-like)**
   - Reminder Agent uses `asyncio.sleep()` for near-term timers (minutes/hours)
   - For daily/weekly cycles: lightweight scheduler that checks every 60s
   - All timers are persisted (survive restart — recalculated from target time on boot)

5. **Human-in-the-loop as async gate**
   - When an agent hits a gate, it emits `HumanApprovalRequired` and the workflow branch pauses
   - The agent does NOT block — it moves on to other events
   - When human approves via dashboard, `HumanApproved` event flows back to the originating agent
   - The agent correlates via `gate_id` and resumes processing

6. **Graceful shutdown**
   - `SIGTERM` → stop accepting new events → finish processing current → flush to SQLite → exit
   - `SIGINT` (Ctrl+C) → same but with 10s timeout before force-exit

---

## 5. Data Model (SQLite)

### Core Tables

```sql
-- Clients & Dogs
clients (id, name, email, phone, address, zone, status, created_at)
dogs (id, client_id, name, breed, size, temperament, special_needs, compatible_with, created_at)

-- Walkers
walkers (id, name, phone, email, zones[], active, created_at)

-- Schedule
walks (id, client_id, dog_id, walker_id, date, start_time, duration, group_id, status, created_at)
walk_groups (id, name, walker_id, date, max_dogs)

-- Invoicing
invoices (id, client_id, period_start, period_end, amount, status, due_date, paid_date, items_json)
payments (id, invoice_id, amount, method, received_at)

-- Communication
messages (id, client_id, direction, channel, subject, body, sent_at, status)
message_templates (id, type, subject, body_template, variables_json)

-- Event Store (durability)
event_store (id, type, payload_json, status, retries, error, created_at, processed_at)

-- Audit Journal
journal (id, event_type, timestamp, actor, details_json, related_booking_id, related_client_id)

-- Human Approval Gates
approval_gates (id, gate_type, context_json, status, created_at, resolved_at, resolution, resolver_notes)

-- Dead Letter Queue
dlq (id, original_event_id, event_type, payload_json, error, failed_at, retries)
```

---

## 6. Tech Stack

| Component | Technology | Rationale |
|---|---|---|
| Language | Python 3.11+ | Async support, Andrey's stack, BuzzBoard precedent |
| Async runtime | asyncio | Native, no external deps, proven at this scale |
| Event bus | Custom (asyncio.Queue + SQLite) | Simple, no Redis/NATS needed for 12 clients. Can upgrade later. |
| Database | SQLite (WAL mode) | Zero-config, file-based, sufficient for this scale |
| Web framework | FastAPI + Uvicorn | Async-native, OpenAPI docs, WebSocket support |
| LLM | Ollama (llama3.1:8b) | Local, private, no API costs, BuzzBoard precedent |
| Email (IMAP) | aioimaplib | Async IMAP client |
| Email (SMTP) | aiosmtplib | Async SMTP client |
| Scheduling | asyncio tasks + persistent timers | No heavy cron dependency |
| Frontend | Jinja2 templates + HTMX | Simple, real-time via WebSocket, no JS framework |
| Config | pydantic-settings + .env | Type-safe, environment-aware |

---

## 7. Project Structure

```
walking-hounds/
├── pyproject.toml
├── .env.example
├── README.md
├── ARCHITECTURE.md            ← this file
├── docs/
│   ├── workflow-diagram.md    ← visual workflow (ASCII + Mermaid)
│   └── api-reference.md       ← REST API docs (auto-generated)
├── src/
│   ├── __init__.py
│   ├── main.py                ← entry point: starts event bus + all agents
│   ├── config.py              ← pydantic-settings, .env loading
│   ├── bus/
│   │   ├── __init__.py
│   │   ├── event.py           ← BaseEvent + all event type definitions (Pydantic)
│   │   ├── bus.py             ← EventBus (publish, subscribe, replay, DLQ)
│   │   └── store.py           ← SQLite event store (durability)
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py            ← BaseAgent (async loop, health, lifecycle)
│   │   ├── intake.py          ← IntakeAgent (email polling, LLM parsing)
│   │   ├── scheduling.py      ← SchedulingAgent (calendar, walker assignment)
│   │   ├── communication.py   ← CommunicationAgent (drafting, sending, gates)
│   │   ├── invoicing.py       ← InvoicingAgent (billing, payment tracking)
│   │   ├── reminder.py        ← ReminderAgent (time-based triggers)
│   │   ├── logger.py          ← LoggerAgent (journal/audit)
│   │   └── dashboard.py       ← DashboardAgent (FastAPI server)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py          ← SQLAlchemy models (or raw SQL)
│   │   ├── database.py        ← async SQLite connection (aiosqlite)
│   │   └── migrations.py      ← schema creation/upgrade
│   ├── email/
│   │   ├── __init__.py
│   │   ├── imap_client.py     ← async IMAP polling
│   │   └── smtp_client.py     ← async SMTP sending
│   ├── llm/
│   │   ├── __init__.py
│   │   └── ollama_client.py   ← async Ollama HTTP client (aiohttp)
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py             ← FastAPI app factory
│   │   ├── routes/
│   │   │   ├── dashboard.py   ← main dashboard view
│   │   │   ├── schedule.py    ← schedule API
│   │   │   ├── approvals.py   ← approval gate API
│   │   │   ├── journal.py     ← journal/audit log API
│   │   │   └── invoices.py    ← invoice API
│   │   ├── templates/         ← Jinja2 templates
│   │   └── static/            ← CSS, JS (HTMX)
│   └── cli.py                 ← Click CLI (start, status, seed-data)
├── tests/
│   ├── test_bus.py
│   ├── test_intake.py
│   ├── test_scheduling.py
│   ├── test_invoicing.py
│   └── ...
└── data/
    └── walking_hounds.db      ← SQLite database (gitignored)
```

---

## 8. Implementation Phases

### Phase 1: Foundation (async event bus + data model)
- Pydantic event types (all events from catalog above)
- EventBus with SQLite-backed durability
- BaseAgent abstract class with async lifecycle
- SQLite schema creation (all tables)
- Config system (.env, pydantic-settings)
- CLI scaffold (Click)
- **Tests:** event publish/subscribe, event durability/replay, agent lifecycle

### Phase 2: Intake Agent (email → events)
- Async IMAP polling (aioimaplib)
- LLM-based email parsing (Ollama: extract intent, client, dog, date/time)
- Intent classification → emit typed events
- Deduplication (Message-ID tracking)
- Confidence threshold → human approval gate
- **Tests:** email parsing, intent classification, dedup, low-confidence gating

### Phase 3: Scheduling Agent (events → schedule)
- Calendar logic (create, cancel, reschedule walks)
- Walker auto-assignment (zone + capacity + dog compatibility)
- Conflict detection and auto-resolution
- Group composition (compatible dogs together, max 4-5 per walker)
- Human escalation for unresolvable conflicts
- **Tests:** booking creation, conflict detection, walker assignment, cancellation

### Phase 4: Communication Agent (events → messages)
- Async SMTP sending (aiosmtplib)
- Template system for confirmations, reminders, follow-ups
- LLM-drafted responses to queries
- Sentiment detection → human gate for complaints
- Message history tracking
- **Tests:** template rendering, SMTP mock, sentiment gating

### Phase 5: Invoicing + Reminder Agents
- Invoice generation (per walk / weekly / monthly — configurable)
- Payment tracking and reminder escalation
- Time-based reminder triggers (walk reminders, walker briefings)
- Persistent timers (survive restart)
- Follow-up feedback requests
- **Tests:** invoice calculation, reminder scheduling, escalation flow

### Phase 6: Dashboard + Logger (web UI + audit)
- FastAPI app with Jinja2 + HTMX
- Dashboard: today's schedule, walker assignments, pending approvals
- Activity journal (filterable by event type, date, client)
- Approval gate UI (approve/reject with notes)
- WebSocket for real-time updates
- Agent health monitoring
- **Tests:** API endpoints, approval flow, journal queries

### Phase 7: Integration + Polish
- End-to-end test: email → booking → confirmation → reminder → walk → invoice → payment
- Dead letter queue dashboard
- Graceful shutdown / restart recovery
- Seed data (12 clients, 3 walkers, sample dogs)
- README with setup instructions
- **Tests:** full workflow integration test

---

## 9. Workflow Schemes

### Happy Path: New Booking via Email

```
Client sends email →
  Intake Agent polls IMAP →
    LLM parses: "Hi, can you walk Bello on Friday at 10am?" →
      Intent: BOOKING, Client: known, Dog: Bello, Date: Fri, Time: 10:00 →
        Emit: BookingIntent →
          Scheduling Agent receives →
            Checks walker availability in client's zone →
              Assigns walker (e.g., Sarah, zone North, 2/4 capacity) →
                Emit: ScheduleConfirmed →
                  Communication Agent receives →
                    Renders confirmation template →
                      Sends email: "Hi, confirmed! Sarah will walk Bello on Friday at 10am." →
                        Emit: ConfirmationSent →
                          Logger records journal entry →
                            Reminder Agent schedules: 2h before walk →
                              Invoicing Agent adds walk to client's invoice →
                                Dashboard updates in real-time
```

### Cancellation Path

```
Client emails: "Sorry, need to cancel Friday's walk for Bello" →
  Intake Agent → Intent: CANCELLATION →
    Emit: CancellationIntent →
      Scheduling Agent receives →
        Removes walk from schedule →
        Checks: is it late cancellation? (< 4h before walk) →
          If yes: flag for potential fee → human review →
          If no: Emit: CancellationConfirmed →
            Communication Agent sends cancellation confirmation →
              Invoicing Agent adjusts invoice →
                Logger records → Dashboard updates
```

### Human Gate: New Client

```
Unknown email: "Hi, I'm new. I have a 2-year-old Labrador named Max..." →
  Intake Agent: client not in database →
    Emit: HumanApprovalRequired (gate_type: new_client) →
      Dashboard shows pending approval with client details →
        Human reviews → clicks "Approve & Assign Walker" →
          Emit: HumanApproved →
            Scheduling Agent creates client + dog record →
              Proceeds with normal booking flow →
                Communication Agent sends welcome + confirmation
```

### Human Gate: Complaint

```
Client emails: "The walker was 20 minutes late and didn't clean up after the dog!" →
  Intake Agent: Intent: COMPLAINT, severity: high →
    Emit: ComplaintIntent →
      Communication Agent receives →
        LLM detects negative sentiment →
          Drafts empathetic response →
            Emit: HumanApprovalRequired (gate_type: complaint_response) →
              Dashboard shows: original message + drafted response →
                Human edits/approves →
                  Communication Agent sends approved response →
                    Logger records
```

---

## 10. Scalability Path

The async event-driven design means scaling up is straightforward:

| Current (12 clients) | Scaling to (50+ clients) |
|---|---|
| Single process, asyncio | Multiple processes (one per agent) communicating via Redis/NATS |
| SQLite | PostgreSQL |
| Local Ollama | Ollama cluster or API-based LLM (OpenRouter) |
| IMAP polling | Webhook-based email (Postmark, SendGrid Inbound Parse) |
| Single dashboard | Role-based dashboard (owner, walker, accountant views) |

The agent boundaries and event contracts don't change — only the transport layer evolves.
