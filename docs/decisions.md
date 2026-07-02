# Walking Hounds — Business Rules & Decisions

> Locked-in decisions from architecture review with Andrey.

---

## 1. Communication

| Decision | Value |
|---|---|
| Email provider | Gmail (IMAP/SMTP) |
| Email address | `andereamru+walking-hounds@gmail.com` (plus-alias sub-addressing) |
| Client communication channel | Email only — no web page for clients |
| Inbound | IMAP polling (async, every 60s) |
| Outbound | SMTP via aiosmtplib |

## 2. Schedule & Operations

| Decision | Value |
|---|---|
| Business days | Monday–Friday (no weekends) |
| Walk time | Lunchtime, staggered across 3 groups |
| Group 1 | 11:30–12:30 |
| Group 2 | 12:00–13:00 |
| Group 3 | 12:30–13:30 |
| Max dogs per group | 4 |
| Max groups per day | 3 (one per walker) |
| Min groups per day | 2 (some days only 2 groups needed) |
| Walkers | 3 |
| Clients | 12 (seed data, fictional) |
| Service type | Standard dog walk (lunchtime only) |
| Zones | Not used for now (flat assignment) |

## 3. Dog Groups — Special Rules

- **Puppy group:** Dogs aged 4–10 months should be grouped together (separate from adult dogs)
- Groups are formed based on: puppy status, dog compatibility, walker capacity
- Auto-assignment logic: puppy group takes priority, then fill remaining groups with adult dogs

## 4. Dog Info Cards

Each dog has a short info card in the system:
- Breed
- Age (months/years)
- Temperament (calm, energetic, anxious, friendly, etc.)
- Sex (male/female)
- Castration status (for males: neutered/intact)
- For females: menstrual cycle status (in heat / not in heat)
- Special needs (medication, behavioral notes — human-only field)

## 5. Cancellation Policy

| Timing | Fee |
|---|---|
| Cancel > 24h before walk | Full refund (no charge) |
| Cancel < 24h before walk | 50% charge |
| No-show | Full charge (100%) |

Late cancellations (< 24h) trigger a human review gate for fee application.

## 6. Invoicing

| Decision | Value |
|---|---|
| Price per walk | €20 (fixed, configurable in .env) |
| Invoice trigger | Immediately after reservation confirmation |
| Invoice delivery | Sent with the booking confirmation email |
| Payment method | Placeholder (test/dummy payment address for now) |
| Payment validation | Human marks as paid via dashboard → signal to Invoicing Agent |
| No automated payment processing | Human confirms payment received |

## 7. LLM

| Decision | Value |
|---|---|
| Provider | Local Ollama |
| Model | llama3.1:8b (same as BuzzBoard) |
| Use cases | Email parsing, intent classification, response drafting, sentiment detection |

## 8. Dashboard

| Decision | Value |
|---|---|
| Framework | FastAPI + Jinja2 + HTMX (same as BuzzBoard) |
| Theme | Dark theme, card-based layout (BuzzBoard-style) |
| Real-time | WebSocket for live updates |
| Human interface | Dashboard is the ONLY human-system interface |
| Features | Schedule view, approval gates, journal, dog info cards, agent health |

## 9. Security & Data

| Decision | Value |
|---|---|
| Secrets | `.env` file (gitignored), placeholders in `.env.example` |
| Database | SQLite (gitignored) |
| Gmail app password | Placeholder in `.env.example`, real value in `.env` only |
| `.gitignore` | `.env`, `*.db`, `data/`, `__pycache__/`, `.venv/` |

## 10. Not Implemented (Deferred)

- Web page for clients (email-only for now)
- Automated payment processing (human validates)
- Zone-based walker assignment
- Weekend operations
- Multiple service types
- Refund processing (human only)
