#!/usr/bin/env python3
"""Enrol, list and remove Telegram alert subscribers for a tenant (operator tool).

Alerts are subscriber-only: the Worker sends an incident to a chat only when the
chat has an enabled subscription for the incident's tenant and exact event type
AND an enabled approval in `approved-destinations`. No application identity may
write approvals (DESTINATION_POLICY.md), so this runs as the operator, with
their own `az login` (Cosmos DB Data Contributor on the account).

    # 1. Enrol one member. Prints a one-time code; the member sends
    #    "/start <code>" to the bot from their own Telegram account. That
    #    message proves they own the chat. Then the approval and the
    #    subscriptions are written and the member gets a confirmation.
    python scripts/telegram-demo-subscribers.py enrol --tenant TENANT-01 --label Malar \
        --events intrusion_detected,fire_detected,smoke_detected,crowd_threshold_exceeded

    python scripts/telegram-demo-subscribers.py list --tenant TENANT-01

    # Stop alerts to one chat (identified by the last digits list shows).
    # Reversible: records are disabled, not deleted.
    python scripts/telegram-demo-subscribers.py remove --tenant TENANT-01 --chat-suffix 508

The bot token is read from Key Vault into memory and never printed. Chat IDs
are printed masked. Nothing is written until ownership is proven.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COSMOS_ENDPOINT = "https://incident-cosmos-4pqaac2c4e4hs.documents.azure.com:443/"
DATABASE = "incidents"
KEY_VAULT = "incidentkv4pqaac2c4e4hs"
BOT_TOKEN_SECRET = "telegram-bot-token"
TENANT = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,39}$")
EVENT = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
LABEL = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,39}$")
# Same namespace idea as the Subscription API's owned subscriptions: one stable
# id per (tenant, chat, event type), so re-running enrol updates, never duplicates.
NAMESPACE = uuid.UUID("3f0b6c2e-8a51-4d7e-9c3a-5e2d1b7f4a60")
ENROL_TIMEOUT_SECONDS = 300


def mask(chat_id: str) -> str:
    return "…" + chat_id[-3:] if len(chat_id) > 3 else "…"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def destination_record(tenant: str, chat_id: str, label: str, enabled: bool = True) -> dict:
    # The fields Worker and Subscription API validate, plus a display label.
    return {"id": "telegram:" + chat_id, "tenant_id": tenant, "channel": "telegram", "telegram_chat_id": chat_id,
            "enabled": enabled, "label": label, "approved_at": now_iso(), "approved_by": "telegram-demo-subscribers"}


def subscription_record(tenant: str, chat_id: str, event_type: str, enabled: bool = True, created_at: str | None = None) -> dict:
    # Exactly the Subscription API's document shape (Subscription.model_dump).
    stamp = now_iso()
    return {"id": "SUB-" + str(uuid.uuid5(NAMESPACE, f"{tenant}|{chat_id}|{event_type}")), "tenant_id": tenant,
            "event_type": event_type, "channel": "telegram", "telegram_chat_id": chat_id, "enabled": enabled,
            "created_at": created_at or stamp, "updated_at": stamp, "owner_object_id": None}


def find_start(updates: list, code: str) -> dict | None:
    """The private chat that sent exactly "/start <code>", if any."""
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and (message.get("text") or "").strip() == f"/start {code}":
            return {"chat_id": str(chat["id"]), "first_name": chat.get("first_name", "")}
    return None


class Telegram:
    def __init__(self, token: str, opener=urlopen):
        self._token, self._open = token, opener

    def call(self, method: str, **params):
        request = Request(f"https://api.telegram.org/bot{self._token}/{method}", data=urlencode(params).encode(), method="POST")
        try:
            with self._open(request, timeout=35) as response:
                body = json.loads(response.read())
        except Exception as exc:  # never echo the URL: it contains the token
            raise SystemExit(f"Telegram {method} failed: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise SystemExit(f"Telegram {method} failed: {body.get('description', 'error')}")
        return body["result"]


def bot_token() -> str:
    result = subprocess.run(["az", "keyvault", "secret", "show", "--vault-name", KEY_VAULT, "--name", BOT_TOKEN_SECRET,
                             "--query", "value", "-o", "tsv"], capture_output=True, text=True, shell=sys.platform == "win32")
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise SystemExit("Could not read the bot token from Key Vault (needs Key Vault Secrets User/Officer).")
    return token


def database():
    from azure.cosmos import CosmosClient
    from azure.identity import AzureCliCredential
    return CosmosClient(COSMOS_ENDPOINT, credential=AzureCliCredential()).get_database_client(DATABASE)


def wait_for_start(telegram: Telegram, code: str, timeout: float = ENROL_TIMEOUT_SECONDS, clock=time.monotonic) -> dict:
    if telegram.call("getWebhookInfo").get("url"):
        # getUpdates is unavailable while a webhook is set; never remove someone else's webhook.
        raise SystemExit("The bot has a webhook configured; enrolment by /start code is unavailable.")
    deadline = clock() + timeout
    while clock() < deadline:
        # Long poll without an offset: reads updates without consuming them.
        found = find_start(telegram.call("getUpdates", timeout=25, allowed_updates='["message"]'), code)
        if found:
            return found
    raise SystemExit("No matching /start message arrived in time. Nothing was written.")


def enrol(args) -> None:
    events = [e.strip() for e in args.events.split(",") if e.strip()]
    if not events or not all(EVENT.fullmatch(e) for e in events):
        raise SystemExit("--events must be comma-separated event types, e.g. intrusion_detected,fire_detected")
    telegram = Telegram(bot_token())
    bot = telegram.call("getMe")["username"]
    code = "LML-" + secrets.token_hex(3).upper()
    print(f"Ask {args.label} to open Telegram, search @{bot}, and send exactly:\n\n    /start {code}\n")
    print(f"Waiting up to {ENROL_TIMEOUT_SECONDS // 60} minutes…")
    found = wait_for_start(telegram, code)
    chat_id = found["chat_id"]
    db = database()
    db.get_container_client("approved-destinations").upsert_item(destination_record(args.tenant, chat_id, args.label))
    subscriptions = db.get_container_client("subscriptions")
    for event in events:
        subscriptions.upsert_item(subscription_record(args.tenant, chat_id, event))
    telegram.call("sendMessage", chat_id=chat_id,
                  text=f"LML SENTINEL: you will now receive {args.tenant} alerts for {', '.join(events)}.")
    print(f"Enrolled {args.label} (chat {mask(chat_id)}) for {len(events)} event types in {args.tenant}.")


def list_subscribers(args) -> None:
    db = database()
    params = [{"name": "@t", "value": args.tenant}]
    approvals = {r["telegram_chat_id"]: r for r in db.get_container_client("approved-destinations").query_items(
        "SELECT * FROM c WHERE c.tenant_id = @t", parameters=params, partition_key=args.tenant)}
    subs = {}
    for row in db.get_container_client("subscriptions").query_items(
            "SELECT * FROM c WHERE c.tenant_id = @t AND IS_DEFINED(c.event_type)", parameters=params, partition_key=args.tenant):
        if row.get("enabled") is True:
            subs.setdefault(row["telegram_chat_id"], []).append(row["event_type"])
    for chat in sorted(set(approvals) | set(subs)):
        approval = approvals.get(chat, {})
        status = "receives alerts" if approval.get("enabled") is True and subs.get(chat) else "no alerts"
        print(f"{mask(chat):6} {approval.get('label', '-'):12} approved={approval.get('enabled') is True!s:5} "
              f"{status:15} {', '.join(sorted(subs.get(chat, []))) or '-'}")


def remove(args) -> None:
    db = database()
    params = [{"name": "@t", "value": args.tenant}]
    approvals = db.get_container_client("approved-destinations")
    matches = [r for r in approvals.query_items("SELECT * FROM c WHERE c.tenant_id = @t", parameters=params, partition_key=args.tenant)
               if r["telegram_chat_id"].endswith(args.chat_suffix)]
    subs = db.get_container_client("subscriptions")
    sub_rows = [r for r in subs.query_items("SELECT * FROM c WHERE c.tenant_id = @t AND IS_DEFINED(c.event_type)",
                                            parameters=params, partition_key=args.tenant)
                if r["telegram_chat_id"].endswith(args.chat_suffix)]
    chats = {r["telegram_chat_id"] for r in matches} | {r["telegram_chat_id"] for r in sub_rows}
    if len(chats) != 1:
        raise SystemExit(f"--chat-suffix must match exactly one chat in {args.tenant}; it matched {len(chats)}.")
    for row in matches:
        approvals.upsert_item({**{k: v for k, v in row.items() if not k.startswith("_")}, "enabled": False})
    for row in sub_rows:
        subs.upsert_item({**{k: v for k, v in row.items() if not k.startswith("_")}, "enabled": False, "updated_at": now_iso()})
    print(f"Disabled chat {mask(chats.pop())}: {len(matches)} approval(s), {len(sub_rows)} subscription(s). Re-run enrol to restore.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("enrol"); p.add_argument("--tenant", required=True); p.add_argument("--label", required=True)
    p.add_argument("--events", required=True); p.set_defaults(func=enrol)
    p = sub.add_parser("list"); p.add_argument("--tenant", required=True); p.set_defaults(func=list_subscribers)
    p = sub.add_parser("remove"); p.add_argument("--tenant", required=True)
    p.add_argument("--chat-suffix", required=True); p.set_defaults(func=remove)
    args = parser.parse_args(argv)
    if not TENANT.fullmatch(args.tenant):
        parser.error("--tenant must look like TENANT-01")
    if getattr(args, "label", None) is not None and not LABEL.fullmatch(args.label):
        parser.error("--label must be a short name (letters, spaces, . ' -)")
    if getattr(args, "chat_suffix", None) is not None and not re.fullmatch(r"[0-9]{3,20}", args.chat_suffix):
        parser.error("--chat-suffix must be 3 or more digits")
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
