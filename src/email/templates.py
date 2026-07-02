"""Email message templates for the Walking Hounds system.

All templates are plain-text functions that return (subject, body) tuples.
Variables are passed as keyword arguments.
"""

from __future__ import annotations


def booking_confirmation(
    *,
    client_name: str,
    dog_name: str,
    walker_name: str,
    walk_date: str,
    walk_slot: str,
    price_eur: float = 20.0,
    payment_address: str = "",
) -> tuple[str, str]:
    """Booking confirmation email."""
    subject = f"✅ Walk confirmed — {dog_name} on {walk_date} at {walk_slot}"
    body = f"""Hi {client_name},

Your walk has been confirmed! Here are the details:

  🐕 Dog:      {dog_name}
  🚶 Walker:   {walker_name}
  📅 Date:     {walk_date}
  ⏰ Time:     {walk_slot}
  💶 Price:    €{price_eur:.2f}

To cancel: Please reply to this email at least 24 hours before the walk
for a full refund. Cancellations within 24 hours are charged at 50%.

Payment: Please send €{price_eur:.2f} to {payment_address}

Thank you for choosing Walking Hounds!

— The Walking Hounds Team
"""
    return subject, body


def cancellation_confirmation(
    *,
    client_name: str,
    dog_name: str,
    walk_date: str,
    refund_percent: int,
    late_cancellation: bool,
) -> tuple[str, str]:
    """Cancellation confirmation email."""
    if late_cancellation:
        refund_text = f"A refund of {refund_percent}% will be processed (late cancellation)."
    else:
        refund_text = "A full refund will be processed."

    subject = f"❌ Walk cancelled — {dog_name} on {walk_date}"
    body = f"""Hi {client_name},

Your walk for {dog_name} on {walk_date} has been cancelled.

{refund_text}

We hope to see you again soon!

— The Walking Hounds Team
"""
    return subject, body


def reschedule_confirmation(
    *,
    client_name: str,
    dog_name: str,
    old_date: str,
    new_date: str,
    new_slot: str,
    walker_name: str,
) -> tuple[str, str]:
    """Reschedule confirmation email."""
    subject = f"🔄 Walk rescheduled — {dog_name} moved to {new_date}"
    body = f"""Hi {client_name},

Your walk for {dog_name} has been rescheduled:

  📅 Was:       {old_date}
  📅 Now:       {new_date} at {new_slot}
  🚶 Walker:    {walker_name}

See you then!

— The Walking Hounds Team
"""
    return subject, body


def clarification_request(
    *,
    client_name: str,
    clarification_text: str,
) -> tuple[str, str]:
    """Clarification request email — sent when the booking email was unclear."""
    subject = "❓ We need a bit more info"
    body = f"""Hi {client_name},

Thank you for your email!

{clarification_text}

Please reply with the missing information so we can process your request.

— The Walking Hounds Team
"""
    return subject, body


def walk_reminder(
    *,
    client_name: str,
    dog_name: str,
    walker_name: str,
    walk_date: str,
    walk_slot: str,
) -> tuple[str, str]:
    """Walk reminder — sent 2 hours before the walk."""
    subject = f"🔔 Walk reminder — {dog_name} today at {walk_slot}"
    body = f"""Hi {client_name},

Just a friendly reminder: {walker_name} will be walking {dog_name} today at {walk_slot}.

See you soon!

— The Walking Hounds Team
"""
    return subject, body


def payment_reminder(
    *,
    client_name: str,
    amount_eur: float,
    due_date: str,
    payment_address: str,
    reminder_count: int,
) -> tuple[str, str]:
    """Payment reminder email."""
    if reminder_count == 1:
        tone = "A gentle reminder that"
    else:
        tone = "This is our second reminder that"

    subject = f"💶 Payment reminder — €{amount_eur:.2f}"
    body = f"""Hi {client_name},

{tone} payment of €{amount_eur:.2f} is due by {due_date}.

Please send payment to: {payment_address}

Thank you!

— The Walking Hounds Team
"""
    return subject, body


def walker_briefing(
    *,
    walker_name: str,
    walk_date: str,
    walks: list[dict],
) -> tuple[str, str]:
    """Morning briefing for a walker — lists all their walks for the day."""
    subject = f"📋 Today's walks — {walk_date}"
    lines = [f"Hi {walker_name},", "", f"Here are your walks for {walk_date}:", ""]

    for i, w in enumerate(walks, 1):
        lines.append(f"  {i}. {w.get('slot', '?')} — {w.get('dog_name', '?')} ({w.get('breed', '?')})")
        if w.get("special_needs"):
            lines.append(f"     ⚠️ Special needs: {w['special_needs']}")

    lines += ["", "Have a great day!", "", "— The Walking Hounds Team"]
    return subject, "\n".join(lines)


def feedback_request(
    *,
    client_name: str,
    dog_name: str,
    walk_date: str,
) -> tuple[str, str]:
    """Post-walk feedback request."""
    subject = f"🐾 How was today's walk with {dog_name}?"
    body = f"""Hi {client_name},

We hope {dog_name} enjoyed today's walk on {walk_date}!

If you have any feedback or concerns, just reply to this email.

— The Walking Hounds Team
"""
    return subject, body


# ── Template registry ──────────────────────────────────────

TEMPLATES = {
    "booking_confirmation": booking_confirmation,
    "cancellation_confirmation": cancellation_confirmation,
    "reschedule_confirmation": reschedule_confirmation,
    "clarification_request": clarification_request,
    "walk_reminder": walk_reminder,
    "payment_reminder": payment_reminder,
    "walker_briefing": walker_briefing,
    "feedback_request": feedback_request,
}
