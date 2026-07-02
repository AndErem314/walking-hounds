# Walking Hounds — Workflow Diagrams

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        WALKING HOUNDS SYSTEM                         │
│                                                                     │
│  ┌──────────┐  email  ┌──────────┐  events  ┌──────────────────┐  │
│  │  IMAP     │────────▶│  INTAKE   │────────▶│  ASYNC EVENT BUS  │  │
│  │  Inbox    │         │  AGENT    │         │  (SQLite-backed)  │  │
│  └──────────┘         └──────────┘         └───────┬──────────┘  │
│                                                     │              │
│                    ┌──────────┬──────────┬──────────┼──────────┐  │
│                    ▼          ▼          ▼          ▼          ▼  │
│              ┌──────────┐┌──────────┐┌──────────┐┌─────────┐┌──────┐│
│              │ SCHEDUL  ││  COMMS   ││ INVOICING││ REMINDER││LOGGER││
│              │  AGENT   ││  AGENT   ││  AGENT   ││  AGENT  ││AGENT ││
│              └────┬─────┘└────┬─────┘└────┬─────┘└────┬────┘└──────┘│
│                   │           │           │           │             │
│                   │      ┌────▼─────┐     │           │             │
│                   │      │   SMTP   │     │           │             │
│                   │      │  Outbox  │     │           │             │
│                   │      └──────────┘     │           │             │
│                   │           │           │           │             │
│                   ▼           ▼           ▼           ▼             │
│              ┌────────────────────────────────────────────────┐     │
│              │              DASHBOARD AGENT (FastAPI)          │     │
│              │  ┌──────────────────────────────────────────┐  │     │
│              │  │  Schedule │ Approvals │ Journal │ Invoices│  │     │
│              │  └──────────────────────────────────────────┘  │     │
│              └────────────────────┬───────────────────────────┘     │
│                                   │                                  │
│                                   ▼                                  │
│                              👤 HUMAN                               │
│                     (approvals, overrides,                         │
│                      emergency response)                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Agent Ownership Map

```
┌─────────────────────────────────────────────────────────────┐
│                     WHAT EACH AGENT OWNS                      │
├──────────────┬──────────────────────────────────────────────┤
│ INTAKE       │ • Email inbox (read-only)                    │
│              │ • Intent classification                      │
│              │ • Client/dog recognition                     │
│              │ • Confidence scoring                         │
├──────────────┼──────────────────────────────────────────────┤
│ SCHEDULING   │ • Walk calendar (CRUD)                       │
│              │ • Walker assignment algorithm                │
│              │ • Group composition                          │
│              │ • Conflict detection & auto-resolution       │
│              │ • Zone management                            │
├──────────────┼──────────────────────────────────────────────┤
│ COMMUNICATION│ • All outbound messages                      │
│              │ • Message templates                          │
│              │ • LLM response drafting                      │
│              │ • Sentiment detection                        │
│              │ • Message history per client                 │
├──────────────┼──────────────────────────────────────────────┤
│ INVOICING    │ • Invoice generation                         │
│              │ • Payment tracking                           │
│              │ • Reminder escalation (1st, 2nd)             │
│              │ • Billing cycle config                       │
│              │ • NEVER: refunds, price changes              │
├──────────────┼──────────────────────────────────────────────┤
│ REMINDER     │ • Time-based triggers                        │
│              │ • Walk reminders (2h before)                 │
│              │ • Walker morning briefings                   │
│              │ • Feedback requests (post-walk)              │
│              │ • Invoice cycle triggers                     │
├──────────────┼──────────────────────────────────────────────┤
│ LOGGER       │ • Audit journal (append-only)                │
│              │ • Event history (queryable)                  │
│              │ • System memory — nothing happens unlogged   │
│              │ • Emits nothing (terminal sink)              │
├──────────────┼──────────────────────────────────────────────┤
│ DASHBOARD    │ • Web UI (FastAPI + HTMX)                    │
│              │ • Approval gate interface                    │
│              │ • Schedule visualization                     │
│              │ • WebSocket real-time updates                │
│              │ • Agent health monitoring                    │
│              │ • ONLY human ↔ system interface              │
└──────────────┴──────────────────────────────────────────────┘
```

---

## 3. Happy Path — New Booking

```
 ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
 │  EMAIL   │     │  INTAKE  │     │  SCHED   │     │  COMMS   │     │  LOGGER  │
 │  INBOX   │     │  AGENT   │     │  AGENT   │     │  AGENT   │     │  AGENT   │
 └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     └─────────┘
      │                │                │                │
      │ 1. "Walk Bello │                │                │
      │    Friday 10am"│                │                │
      │───────────────▶│                │                │
      │                │                │                │
      │           2. LLM parse         │                │
      │           intent=BOOKING       │                │
      │           conf=0.92            │                │
      │                │                │                │
      │           3. BookingIntent     │                │
      │                │───────────────▶│                │
      │                │                │                │
      │                │           4. Check walker      │
      │                │           availability zone    │
      │                │           Assign: Sarah (2/4)  │
      │                │                │                │
      │                │           5. ScheduleConfirmed │
      │                │                │───────────────▶│
      │                │                │                │
      │                │                │           6. Render template
      │                │                │           "Confirmed! Sarah
      │                │                │            will walk Bello"
      │                │                │                │
      │                │                │           7. Send via SMTP
      │                │                │                │
      │                │                │           8. ConfirmationSent
      │                │                │                │──────────▶│ LOG
      │                │                │                │           │
      │                │                │                │      9. Journal entry
      │                │                │                │           │ "Booking confirmed
      │                │                │                │           │  for Bello, Fri 10am"
      │                │                │                │           │
      │                │                │                │      ┌──────────────────┐
      │                │                │                │      │  ALSO TRIGGERS:  │
      │                │                │                │      │ • Reminder: 2h   │
      │                │                │                │      │   before walk    │
      │                │                │                │      │ • Invoice: add   │
      │                │                │                │      │   walk to bill   │
      │                │                │                │      │ • Dashboard:     │
      │                │                │                │      │   real-time UI   │
      │                │                │                │      └──────────────────┘
```

---

## 4. Cancellation Path

```
 ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
 │  EMAIL   │     │  INTAKE  │     │  SCHED   │     │  COMMS   │     │ INVOICE │
 └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     └─────────┘
      │                │                │                │
      │ "Cancel Bello  │                │                │
      │  Friday walk"  │                │                │
      │───────────────▶│                │                │
      │                │                │                │
      │           CancellationIntent    │                │
      │                │───────────────▶│                │
      │                │                │                │
      │                │           Remove from schedule  │
      │                │           Check: late cancel?   │
      │                │                │                │
      │                │         ┌──────┴───────┐        │
      │                │         │              │        │
      │                │    < 4h before?    >= 4h?       │
      │                │         │              │        │
      │                │    HUMAN GATE    Auto-cancel    │
      │                │    (fee review)       │        │
      │                │         │              │        │
      │                │         │    CancellationConfirmed
      │                │         │              │────────▶│
      │                │         │              │        │
      │                │         │              │   Adjust invoice
      │                │         │              │   (remove walk charge)
      │                │         │              │        │
      │                │         │              │────────▶│ COMMS
      │                │         │              │        │
      │                │         │              │   Send cancellation
      │                │         │              │   confirmation email
```

---

## 5. Human Gate — New Client

```
 ┌─────────┐     ┌─────────┐     ┌──────────────────┐     ┌─────────┐
 │  EMAIL   │     │  INTAKE  │     │   DASHBOARD       │     │  SCHED   │
 └────┬─────┘     └────┬─────┘     └───────┬──────────┘     └────┬─────┘
      │                │                   │                     │
      │ "Hi, I'm new.  │                   │                     │
      │  Labrador Max" │                   │                     │
      │───────────────▶│                   │                     │
      │                │                   │                     │
      │           Client NOT in DB         │                     │
      │           → HumanApprovalRequired  │                     │
      │                │                   │                     │
      │                │──────────────────▶│                     │
      │                │                   │                     │
      │                │          ┌────────┴────────┐            │
      │                │          │  👤 HUMAN REVIEW │            │
      │                │          │                 │            │
      │                │          │ "New client:     │            │
      │                │          │  John Doe        │            │
      │                │          │  Dog: Max (Lab)  │            │
      │                │          │  Zone: North     │            │
      │                │          │  Requested: Fri  │            │
      │                │          │  10am"           │            │
      │                │          │                 │            │
      │                │          │ [Approve] [Reject]│           │
      │                │          └────────┬────────┘            │
      │                │                   │                     │
      │                │           HumanApproved                 │
      │                │                   │────────────────────▶│
      │                │                   │                     │
      │                │                   │              Create client+dog
      │                │                   │              Proceed with booking
      │                │                   │              → ScheduleConfirmed
      │                │                   │              → Comms sends welcome
```

---

## 6. Human Gate — Complaint / Negative Sentiment

```
 ┌─────────┐     ┌─────────┐     ┌──────────────────┐     ┌─────────┐
 │  EMAIL   │     │  INTAKE  │     │   COMMS AGENT     │     │DASHBOARD│
 └────┬─────┘     └────┬─────┘     └───────┬──────────┘     └────┬─────┘
      │                │                   │                     │
      │ "Walker was 20 │                   │                     │
      │  min late!"    │                   │                     │
      │───────────────▶│                   │                     │
      │                │                   │                     │
      │           ComplaintIntent          │                     │
      │           severity=HIGH            │                     │
      │                │──────────────────▶│                     │
      │                │                   │                     │
      │                │           LLM detects negative         │
      │                │           sentiment → DRAFT response    │
      │                │           "I'm so sorry about           │
      │                │            the delay today..."          │
      │                │                   │                     │
      │                │           HumanApprovalRequired         │
      │                │           (gate_type: complaint)        │
      │                │                   │────────────────────▶│
      │                │                   │                     │
      │                │                   │            ┌────────┴────────┐
      │                │                   │            │  👤 HUMAN REVIEW │
      │                │                   │            │                 │
      │                │                   │            │ Original msg +  │
      │                │                   │            │ drafted reply   │
      │                │                   │            │                 │
      │                │                   │            │ [Edit] [Approve]│
      │                │                   │            │ [Reject]        │
      │                │                   │            └────────┬────────┘
      │                │                   │                     │
      │                │                   │◀────HumanApproved───│
      │                │                   │                     │
      │                │           Send APPROVED response         │
      │                │           via SMTP                       │
      │                │                   │                     │
      │                │                   │──→ Logger records   │
```

---

## 7. Async Event Flow (Under the Hood)

```
                    ┌─────────────────────────────────────┐
                    │         ASYNC EVENT BUS              │
                    │                                     │
                    │   publish(event) ──▶ SQLite WRITE   │
                    │                    (durable, WAL)    │
                    │                          │           │
                    │                          ▼           │
                    │   ┌──────────────────────────────┐   │
                    │   │  Dispatch to subscribers     │   │
                    │   │  (asyncio.Queue per agent)   │   │
                    │   └──────────────────────────────┘   │
                    │      │          │          │         │
                    │      ▼          ▼          ▼         │
                    │   [Queue A]  [Queue B]  [Queue C]    │
                    │   bounded    bounded    bounded      │
                    │   (max 100)  (max 50)   (max 200)    │
                    │      │          │          │         │
                    │   if full → publisher AWAITS         │
                    │   (natural backpressure)             │
                    └─────────────────────────────────────┘
                              │          │          │
                              ▼          ▼          ▼
                          ┌──────┐  ┌──────┐  ┌──────┐
                          │Agent │  │Agent │  │Agent │
                          │  A   │  │  B   │  │  C   │
                          │async │  │async │  │async │
                          │ loop │  │ loop │  │ loop │
                          └──────┘  └──────┘  └──────┘
                              │          │          │
                          failed?    failed?    failed?
                              │          │          │
                              ▼          ▼          ▼
                          ┌──────────────────────────────┐
                          │     RETRY (max 3)            │
                          │   with exponential backoff   │
                          │     1s → 2s → 4s             │
                          └──────────┬───────────────────┘
                                     │ still failing?
                                     ▼
                          ┌──────────────────────────────┐
                          │    DEAD LETTER QUEUE         │
                          │   (SQLite table, visible     │
                          │    on dashboard for review)  │
                          └──────────────────────────────┘
```

---

## 8. Daily Lifecycle (Autopilot View)

```
 06:00  │ ┌─ Reminder Agent: morning briefing to walkers (today's schedule)
        │ │
 08:00  │ │ Walk group 1 (Sarah, zone North, 4 dogs)
 10:00  │ │ Walk group 2 (Mike, zone South, 3 dogs)
 14:00  │ │ Walk group 3 (Emma, zone Central, 5 dogs)
        │ │
 16:00  │ ├─ Reminder Agent: send feedback request to morning clients
        │ │
 ALL DAY│ ├─ Intake Agent: polling IMAP every 60s
        │ │   → new bookings, cancellations, queries processed in real-time
        │ │
 18:00  │ ├─ Invoicing Agent: check for overdue invoices → send reminders
        │ │
 20:00  │ ├─ Reminder Agent: next-day schedule confirmation to clients
        │ │
 22:00  │ └─ Logger Agent: daily summary journal entry
        │
  HUMAN │   Checks dashboard 1-2x/day:
        │   • Approve any pending gates (new clients, complaints)
        │   • Review DLQ items if any
        │   • Override schedule if needed
        │   Total human time: ~15-20 min/day
```

---

## 9. What Never Gets Automated

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    HUMAN-ONLY ZONE                           │
  │                                                             │
  │  ✋ Physical dog walking          → Safety: living animals  │
  │  ✋ New client vetting            → Judgment: temperament   │
  │  ✋ Contract signing              → Legal binding            │
  │  ✋ Refunds / price adjustments   → Financial liability     │
  │  ✋ Emergency response            → Time-critical judgment  │
  │  ✋ Walker hiring/firing          → HR decision              │
  │  ✋ Legal complaints              → Legal risk               │
  │  ✋ Medication instructions       → Health liability         │
  │                                                             │
  │  The system is designed to NEVER attempt these.             │
  │  They are not gates — they are hard boundaries.             │
  └─────────────────────────────────────────────────────────────┘
```
