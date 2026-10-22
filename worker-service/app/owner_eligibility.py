"""Delivery eligibility for subscriptions a person created through account setup.

A legacy subscription (no owner_object_id) is unaffected. An owned one is
delivered only while, right now:

  * the owner's central access record is active and still assigns the tenant
    (revoking or disabling the user, or removing the tenant, stops delivery);
  * the owner's verified Telegram binding is the chat being delivered to
    (re-verifying a different account strands the old destination).

Records are read from the Subscription API's reserved platform partition in
the same subscriptions container. A missing record is ineligible; a read
failure raises, so the caller's retry/dead-letter handling applies and an
outage never becomes a send.

notification-service/app/delivery_eligibility.py applies the identical rule
again immediately before sending; tests/local_integration checks they agree.
"""

ACCESS_PARTITION = "__platform_access__"
ROLES = frozenset({"Platform Admin", "SOC Operator", "SOC Viewer"})


def access_record_allows(record, owner_object_id: str, tenant_id: str) -> bool:
    return (isinstance(record, dict) and record.get("record_type") == "platform_access"
            and record.get("object_id") == owner_object_id and record.get("status") == "active"
            and record.get("role") in ROLES and isinstance(record.get("tenants"), list)
            and tenant_id in record["tenants"])


def binding_allows(record, owner_object_id: str, chat_id: str) -> bool:
    return (isinstance(record, dict) and record.get("record_type") == "telegram_owner"
            and record.get("object_id") == owner_object_id and record.get("chat_id") == chat_id)


async def owner_eligible(read_platform_record, owner_object_id: str, tenant_id: str, chat_id: str) -> bool:
    access = await read_platform_record("access:" + owner_object_id)
    if not access_record_allows(access, owner_object_id, tenant_id):
        return False
    return binding_allows(await read_platform_record("telegram-owner:" + owner_object_id), owner_object_id, chat_id)
