"""Tests for email templates — verifies content and required fields."""

from __future__ import annotations

import pytest

from src.email.templates import (
    TEMPLATES,
    booking_confirmation,
    cancellation_confirmation,
    reschedule_confirmation,
    clarification_request,
    walk_reminder,
    payment_reminder,
    walker_briefing,
    feedback_request,
)


class TestBookingConfirmation:
    def test_returns_subject_and_body(self):
        subject, body = booking_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        )
        assert "confirmed" in subject.lower()
        assert "Bello" in body
        assert "Sarah" in body
        assert "2025-07-04" in body
        assert "11:30" in body

    def test_includes_price(self):
        subject, body = booking_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
            price_eur=20.0,
        )
        assert "€20.00" in body

    def test_includes_cancellation_policy(self):
        subject, body = booking_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        )
        assert "24 hours" in body
        assert "50%" in body

    def test_includes_payment_address(self):
        subject, body = booking_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
            payment_address="test@payment.local",
        )
        assert "test@payment.local" in body


class TestCancellationConfirmation:
    def test_full_refund_text(self):
        subject, body = cancellation_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walk_date="2025-07-04",
            refund_percent=100,
            late_cancellation=False,
        )
        assert "cancelled" in subject.lower()
        assert "full refund" in body.lower()

    def test_partial_refund_text(self):
        subject, body = cancellation_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            walk_date="2025-07-04",
            refund_percent=50,
            late_cancellation=True,
        )
        assert "50%" in body
        assert "late cancellation" in body.lower()


class TestRescheduleConfirmation:
    def test_shows_old_and_new_dates(self):
        subject, body = reschedule_confirmation(
            client_name="Lisa",
            dog_name="Bello",
            old_date="2025-07-04",
            new_date="2025-07-11",
            new_slot="12:00",
            walker_name="Sarah",
        )
        assert "rescheduled" in subject.lower()
        assert "2025-07-04" in body
        assert "2025-07-11" in body
        assert "12:00" in body


class TestClarificationRequest:
    def test_includes_clarification_text(self):
        subject, body = clarification_request(
            client_name="Lisa",
            clarification_text="your dog's name and the specific date",
        )
        assert "more info" in subject.lower()
        assert "your dog's name" in body
        assert "specific date" in body


class TestWalkReminder:
    def test_includes_walk_details(self):
        subject, body = walk_reminder(
            client_name="Lisa",
            dog_name="Bello",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        )
        assert "reminder" in subject.lower()
        assert "Bello" in body
        assert "Sarah" in body
        assert "11:30" in body


class TestPaymentReminder:
    def test_first_reminder_tone(self):
        subject, body = payment_reminder(
            client_name="Lisa",
            amount_eur=20.0,
            due_date="2025-07-10",
            payment_address="test@pay.local",
            reminder_count=1,
        )
        assert "gentle reminder" in body

    def test_second_reminder_tone(self):
        subject, body = payment_reminder(
            client_name="Lisa",
            amount_eur=20.0,
            due_date="2025-07-10",
            payment_address="test@pay.local",
            reminder_count=2,
        )
        assert "second reminder" in body


class TestWalkerBriefing:
    def test_lists_all_walks(self):
        subject, body = walker_briefing(
            walker_name="Sarah",
            walk_date="2025-07-04",
            walks=[
                {"slot": "11:30", "dog_name": "Bello", "breed": "Labrador"},
                {"slot": "12:00", "dog_name": "Luna", "breed": "Retriever"},
            ],
        )
        assert "Bello" in body
        assert "Luna" in body
        assert "Labrador" in body

    def test_shows_special_needs(self):
        subject, body = walker_briefing(
            walker_name="Sarah",
            walk_date="2025-07-04",
            walks=[
                {"slot": "11:30", "dog_name": "Bello", "breed": "Labrador", "special_needs": "Medication after walk"},
            ],
        )
        assert "Medication" in body
        assert "⚠️" in body


class TestFeedbackRequest:
    def test_includes_dog_name(self):
        subject, body = feedback_request(
            client_name="Lisa",
            dog_name="Bello",
            walk_date="2025-07-04",
        )
        assert "Bello" in subject
        assert "Bello" in body


class TestTemplateRegistry:
    def test_all_templates_registered(self):
        expected = {
            "booking_confirmation",
            "cancellation_confirmation",
            "reschedule_confirmation",
            "clarification_request",
            "walk_reminder",
            "payment_reminder",
            "walker_briefing",
            "feedback_request",
            "onboarding_welcome",
        }
        assert expected == set(TEMPLATES.keys())
