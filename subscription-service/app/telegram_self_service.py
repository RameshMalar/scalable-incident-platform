"""Telegram self-service for signed-in users (contract self-service-v1).

Reviewed from business-dashboard/review-proposals/telegram_lifecycle.py.

  * Ownership: the API issues a one-time /start link. Only a private message
    from that Telegram account, delivered through the bot webhook with the
    configured secret header, binds the account to the signed-in person.
  * Opt-in is separate: a verified binding starts with nothing enabled, and
    linking never sends a message or creates a subscription.
  * Preferences are ordinary subscriptions in the tenant partition, marked with
    owner_object_id, so the existing Worker routing delivers them. Enabling also
    requires the operator-approved destination policy, as for any subscription.
  * Unsubscribing is always allowed, even after ownership or access is lost.

The browser never sees a chat ID or bot token, and every route derives the
person from their delegated token, never from the request.
"""

import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from app.access_registry import RegistryDenied, RegistryUnavailable
from app.models import Subscription
from app.platform_documents import ACCESS_PARTITION, DocumentConflict
from app.rate_limit import SlidingWindowLimiter

CONTRACT = "self-service-v1"
CHALLENGE_SECONDS = 300
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
DEFAULT_EVENT_TYPES = ("intrusion_detected", "helmet_missing", "crowd_threshold_exceeded", "fire_detected", "smoke_detected")
# Fixed namespace so a person's subscription for one tenant and event type has
# one stable id; repeated saves update it instead of piling up duplicates.
OWNED_NAMESPACE = uuid.UUID("7d3c1f0e-5b8a-4c62-9e1d-2a6f4b8c0e13")


class OwnershipRejected(Exception):
    """The update is not a valid ownership confirmation; acknowledge and drop it."""


class RateLimited(Exception):
    """Too many requests; the caller should retry later (HTTP 429)."""


# (limit, window seconds). Generous for people, tight for automation.
CHALLENGES_PER_USER = (5, 900)
UPDATES_PER_SENDER = (10, 60)
UPDATES_TOTAL = (120, 60)
UNAUTHENTICATED_WEBHOOK_CALLS = (30, 60)


def owner_id(object_id):
    return "telegram-owner:" + object_id


def chat_claim_id(chat_id):
    return "telegram-chat:" + chat_id


def challenge_id(object_id):
    return "telegram-challenge:" + object_id


def owned_subscription_id(tenant_id, object_id, event_type):
    return "SUB-" + str(uuid.uuid5(OWNED_NAMESPACE, "\n".join((tenant_id, object_id, event_type))))


class TelegramSelfService:
    def __init__(self, registry, documents, subscriptions, policy, bot_username, webhook_secret, event_types=DEFAULT_EVENT_TYPES, clock=time.time, limit_clock=time.monotonic):
        if not bot_username or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username):
            raise ValueError("TELEGRAM_BOT_USERNAME is required")
        if not isinstance(webhook_secret, str) or len(webhook_secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", webhook_secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be at least 32 of A-Z, a-z, 0-9, _ and -")
        self.registry = registry
        self.documents = documents
        self.subscriptions = subscriptions
        self.policy = policy
        self.bot_username = bot_username
        self.webhook_secret = webhook_secret
        self.events = tuple(sorted(set(event_types)))
        if not self.events:
            raise ValueError("at least one supported event type is required")
        self.clock = clock
        self.challenge_limit = SlidingWindowLimiter(*CHALLENGES_PER_USER, clock=limit_clock)
        self.sender_limit = SlidingWindowLimiter(*UPDATES_PER_SENDER, clock=limit_clock)
        self.update_limit = SlidingWindowLimiter(*UPDATES_TOTAL, clock=limit_clock)
        self.unauthenticated_limit = SlidingWindowLimiter(*UNAUTHENTICATED_WEBHOOK_CALLS, clock=limit_clock)

    def admit_unauthenticated(self):
        # Rejecting a wrong secret is cheap, but a flood still earns a 429.
        if not self.unauthenticated_limit.allow("webhook"):
            raise RateLimited("too many unauthenticated webhook calls")

    def admit_update(self, update) -> bool:
        """False: drop this sender's update quietly. Raises when saturated overall."""
        if not self.update_limit.allow("all"):
            raise RateLimited("too many webhook updates")
        message = update.get("message") if isinstance(update, dict) else None
        sender = message.get("from") if isinstance(message, dict) else None
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        return self.sender_limit.allow(sender_id if type(sender_id) is int else "unknown")

    async def _owner(self, object_id, tenant_id):
        return await self.registry.authorize(object_id, "Subscriptions.SelfManage", tenant_id)

    async def _read(self, document_id):
        try:
            return await self.documents.read(document_id)
        except Exception:
            raise RegistryUnavailable("account records unavailable") from None

    async def status(self, object_id, tenant_id):
        await self._owner(object_id, tenant_id)
        binding = await self._read(owner_id(object_id))
        owned = await self.subscriptions.list_owned(tenant_id, object_id)
        chat = binding["chat_id"] if binding else None
        approved = bool(chat) and (await self.policy.approved(tenant_id, chat)) is True
        return {
            "contract": CONTRACT, "tenant_id": tenant_id,
            "verified": chat is not None,
            "destination_approved": approved,
            # Enabled means a delivery could actually happen: a subscription
            # for the currently verified chat is on. A chat that has since been
            # replaced by a new verification reads as off until saved again.
            "enabled": any(item.enabled and item.telegram_chat_id == chat for item in owned),
            "event_types": sorted(item.event_type for item in owned),
            "supported_event_types": list(self.events),
        }

    async def challenge(self, object_id, tenant_id):
        await self._owner(object_id, tenant_id)
        if not self.challenge_limit.allow(object_id):
            raise RateLimited("too many ownership links requested")
        token =secrets.token_urlsafe(32)  # 43 characters; /start allows 64.
        expires_at = int(self.clock()) + CHALLENGE_SECONDS
        record = {"id": challenge_id(object_id), "tenant_id": ACCESS_PARTITION, "record_type": "telegram_challenge",
                  "object_id": object_id, "platform_tenant": tenant_id, "expires_at": expires_at,
                  "digest": hashlib.sha256(token.encode()).hexdigest()}
        before = await self._read(challenge_id(object_id))
        # One outstanding link per person: a new link replaces the old one.
        operation = ("create", record) if before is None else ("replace", record, before["_etag"])
        try:
            await self.documents.batch([operation])
        except DocumentConflict:
            raise RegistryUnavailable("challenge changed concurrently; retry") from None
        except Exception:
            raise RegistryUnavailable("account records unavailable") from None
        return {"deep_link": f"https://t.me/{self.bot_username}?start={token}", "expires_at": expires_at}

    def webhook_authentic(self, secret) -> bool:
        return isinstance(secret, str) and hmac.compare_digest(secret.encode(), self.webhook_secret.encode())

    async def confirm_ownership(self, update):
        """Bind the private Telegram sender to the person who requested the link."""
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            raise OwnershipRejected("not a message")
        sender, chat, text = message.get("from") or {}, message.get("chat") or {}, message.get("text")
        if (not isinstance(sender, dict) or not isinstance(chat, dict) or chat.get("type") != "private"
                or type(sender.get("id")) is not int or sender.get("is_bot") is not False
                or chat.get("id") != sender["id"] or not isinstance(text, str) or not text.startswith("/start ")):
            raise OwnershipRejected("a private /start confirmation is required")
        token = text[len("/start "):]
        if not TOKEN.fullmatch(token):
            raise OwnershipRejected("invalid challenge")
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            matches = await self.documents.query("telegram_challenge", "digest", digest)
        except Exception:
            raise RegistryUnavailable("account records unavailable") from None
        if len(matches) != 1 or type(matches[0].get("expires_at")) is not int or matches[0]["expires_at"] <= self.clock():
            raise OwnershipRejected("challenge expired or already used")
        challenge = matches[0]
        object_id = challenge["object_id"]
        try:
            await self._owner(object_id, challenge["platform_tenant"])
        except RegistryDenied:
            raise OwnershipRejected("requester is no longer authorized") from None
        chat_id = str(sender["id"])
        before = await self._read(owner_id(object_id))
        claim = await self._read(chat_claim_id(chat_id))
        if claim is not None and claim.get("object_id") != object_id:
            raise OwnershipRejected("this Telegram account belongs to another user")
        now = datetime.now(timezone.utc).isoformat()
        binding = {"id": owner_id(object_id), "tenant_id": ACCESS_PARTITION, "record_type": "telegram_owner",
                   "object_id": object_id, "chat_id": chat_id, "verified_at": now}
        # Consuming the challenge and binding happen in one batch, so a replayed
        # or concurrent confirmation fails its ETag precondition.
        operations = [("delete", challenge["id"], challenge["_etag"]),
                      ("create", binding) if before is None else ("replace", binding, before["_etag"])]
        if before is not None and before.get("chat_id") != chat_id:
            old_claim = await self._read(chat_claim_id(before["chat_id"]))
            if old_claim is not None:
                operations.append(("delete", old_claim["id"], old_claim["_etag"]))
        if claim is None:
            operations.append(("create", {"id": chat_claim_id(chat_id), "tenant_id": ACCESS_PARTITION,
                                          "record_type": "telegram_chat_claim", "object_id": object_id}))
        try:
            await self.documents.batch(operations)
        except DocumentConflict:
            raise OwnershipRejected("challenge already used") from None
        except Exception:
            raise RegistryUnavailable("account records unavailable") from None

    async def pending_destinations(self, actor, tenant_id):
        """Verified, authorized people whose chat is not yet an approved destination.

        Read-only. This API still cannot write approved-destinations: the
        Platform Admin reviews here, and the operator records the approval
        through the existing separate process, so the identity that writes
        subscriptions never approves its own destinations. Returning chat IDs
        is audited first; if the audit cannot be written nothing is returned.
        """
        await self.registry.authorize(actor, "Access.Manage", tenant_id)
        try:
            bindings = await self.documents.query("telegram_owner")
        except Exception:
            raise RegistryUnavailable("account records unavailable") from None
        items = []
        for binding in bindings:
            try:
                grant = await self.registry.resolve(binding.get("object_id"))
            except RegistryDenied:
                continue  # unknown, disabled or revoked people are never proposed
            chat = binding.get("chat_id")
            if tenant_id not in grant.tenants or not isinstance(chat, str):
                continue
            if (await self.policy.approved(tenant_id, chat)) is True:
                continue
            items.append({
                "object_id": grant.object_id, "role": grant.role.value, "verified_at": binding.get("verified_at"),
                "chat_hint": "…" + chat[-3:],
                # Exactly the record DESTINATION_POLICY.md asks the operator to write.
                "approval_record": {"id": "telegram:" + chat, "tenant_id": tenant_id, "channel": "telegram",
                                    "telegram_chat_id": chat, "enabled": True},
            })
        if items:
            audit = {"id": "destination-review:" + uuid.uuid4().hex, "tenant_id": ACCESS_PARTITION,
                     "record_type": "access_audit", "action": "destination_review", "actor_object_id": actor,
                     "platform_tenant": tenant_id, "disclosed_object_ids": [item["object_id"] for item in items],
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            try:
                await self.documents.batch([("create", audit)])
            except Exception:
                raise RegistryUnavailable("review could not be audited") from None
        return {"contract": "destination-review-v1", "tenant_id": tenant_id, "items": items}

    async def save_preferences(self, object_id, tenant_id, enabled, event_types):
        await self._owner(object_id, tenant_id)
        if type(enabled) is not bool or not isinstance(event_types, list) or not set(event_types).issubset(self.events):
            raise ValueError("invalid notification preferences")
        selected = set(event_types)
        binding = await self._read(owner_id(object_id))
        chat = binding["chat_id"] if binding else None
        if enabled:
            if chat is None:
                raise RegistryDenied("verify Telegram ownership before opting in")
            if (await self.policy.approved(tenant_id, chat)) is not True:
                raise RegistryDenied("destination not approved")
        owned = {item.event_type: item for item in await self.subscriptions.list_owned(tenant_id, object_id)}
        now = datetime.now(timezone.utc)
        # Idempotent: each write sets the target state, so a retried PUT after
        # a partial failure converges. Disabling never needs ownership.
        for event_type, item in owned.items():
            if event_type not in selected:
                await self.subscriptions.delete(tenant_id, item.id)
        for event_type in sorted(selected):
            item = owned.get(event_type)
            if item is not None:
                update = {"enabled": enabled, "updated_at": now}
                if enabled:
                    update["telegram_chat_id"] = chat
                await self.subscriptions.replace(item.model_copy(update=update))
            elif chat is not None:
                await self.subscriptions.create(Subscription(
                    id=owned_subscription_id(tenant_id, object_id, event_type), tenant_id=tenant_id,
                    event_type=event_type, channel="telegram", telegram_chat_id=chat, enabled=enabled,
                    created_at=now, updated_at=now, owner_object_id=object_id))
        return await self.status(object_id, tenant_id)
