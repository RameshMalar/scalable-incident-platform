"""The three-member demo: subscriber-only routing decided in the Worker.

Members A and B are approved and subscribed; member C is approved but not
subscribed. The tool-created records (scripts/telegram-demo-subscribers.py)
have exactly this shape.
"""
import logging

import pytest

from common_event_model import IncidentEvent
from app.subscription_router import SubscriptionRouter
from app.subscription_store import InMemorySubscriptionReader

CHAT_A, CHAT_B, CHAT_C = "5550001001", "5550002002", "5550003003"
APPROVED = {("TENANT-01", CHAT_A), ("TENANT-01", CHAT_B), ("TENANT-01", CHAT_C)}


class Approvals:
    async def approved(self, tenant_id, chat_id):
        return (tenant_id, chat_id) in APPROVED


def subscription(chat, event_type, tenant="TENANT-01", enabled=True):
    return {"id": f"SUB-{chat}-{event_type}", "tenant_id": tenant, "event_type": event_type, "channel": "telegram",
            "telegram_chat_id": chat, "enabled": enabled, "created_at": "2026-10-21T00:00:00+00:00",
            "updated_at": "2026-10-21T00:00:00+00:00", "owner_object_id": None}


def router():
    return SubscriptionRouter(InMemorySubscriptionReader([
        subscription(CHAT_A, "intrusion_detected"), subscription(CHAT_A, "fire_detected"),
        subscription(CHAT_B, "intrusion_detected"),
        # Member C: approved destination, but no subscription at all.
    ]), policies=Approvals())


def event(purpose="e2e_test", event_type="intrusion_detected", analytics="intrusion_detection", tenant="TENANT-01"):
    return IncidentEvent(event_id="EVT-SIM-0A1B2C3D", tenant_id=tenant, site_id="SITE-01", source_id="CAM-101",
                         source_name="Camera CAM-101", source_type="video_analytics", analytics_type=analytics,
                         event_type=event_type, severity="high", timestamp="2026-10-21T09:00:00+00:00",
                         event_purpose=purpose, description="test")


async def test_two_subscribed_members_are_alerted_and_the_unsubscribed_one_is_not():
    routes = await router().route(event())
    assert sorted(r.telegram_chat_id for r in routes) == [CHAT_A, CHAT_B]
    assert CHAT_C not in {r.telegram_chat_id for r in routes}
    assert all(r.incident.event_purpose == "e2e_test" for r in routes)


async def test_event_type_filtering_is_exact():
    assert [r.telegram_chat_id for r in await router().route(event(event_type="fire_detected", analytics="fire_smoke_detection"))] == [CHAT_A]
    assert await router().route(event(event_type="smoke_detected", analytics="fire_smoke_detection")) == []


async def test_custom_analytics_reach_only_their_subscribers():
    # A new analytics type with no subscription is stored, but alerts nobody.
    assert await router().route(event(event_type="wrong_way_driving", analytics="vehicle_wrong_way_detection")) == []


async def test_load_test_events_never_reach_telegram():
    assert await router().route(event(purpose="load_test")) == []


async def test_other_tenants_never_receive_these_members():
    assert await router().route(event(tenant="TENANT-02")) == []


async def test_unapproved_chat_is_blocked_even_when_subscribed():
    reader = InMemorySubscriptionReader([subscription("5559999999", "intrusion_detected")])
    assert await SubscriptionRouter(reader, policies=Approvals()).route(event()) == []


async def test_routing_logs_never_contain_chat_ids(caplog):
    reader = InMemorySubscriptionReader([subscription("5559999999", "intrusion_detected"), subscription(CHAT_A, "intrusion_detected")])
    with caplog.at_level(logging.DEBUG):
        await SubscriptionRouter(reader, policies=Approvals()).route(event())
    assert "not approved" in caplog.text  # the blocked route is reported...
    assert "5559999999" not in caplog.text and CHAT_A not in caplog.text  # ...without the chat
