"""Offline, fail-closed checks for CD inputs/configuration; never prints values."""
import os
import re
import sys
from urllib.parse import urlparse
from uuid import UUID


def required(config, name):
    value = config.get(name, "")
    if not value or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Missing or unsafe {name}")
    return value


def directory_id(config, name):
    value = required(config, name)
    try:
        UUID(value)
    except ValueError:
        raise ValueError(f"{name} must be a directory/client/subscription UUID") from None
    return value


def mapping(config, name):
    result = {}
    for entry in required(config, name).split(","):
        if not entry.strip():
            continue
        client, separator, tenant = entry.partition(":")
        client, tenant = client.strip(), tenant.strip()
        if not separator or not tenant or client in result:
            raise ValueError(f"{name} contains an invalid or duplicate client mapping")
        directory_id({"client": client}, "client")
        result[client] = tenant
    if not result:
        raise ValueError(f"{name} is empty")
    return result


def validate(config, *, inputs_only=False):
    if required(config, "DEPLOY_REF") != "refs/heads/main":
        raise ValueError("CD is restricted to an explicitly dispatched main revision")
    if config.get("SUBSCRIPTION_CONFIGURATION_APPROVED") != "true":
        raise ValueError("Subscription roles/scopes, consent and configuration require explicit review")
    deployment = required(config, "CORE_DEPLOYMENT_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,64}", deployment):
        raise ValueError("CORE_DEPLOYMENT_NAME must identify an approved existing deployment")
    directory_id(config, "SUBSCRIPTION_AUTH_TENANT_ID")
    audience = required(config, "SUBSCRIPTION_AUTH_AUDIENCE")
    if any(c.isspace() for c in audience):
        raise ValueError("SUBSCRIPTION_AUTH_AUDIENCE cannot contain whitespace")
    managers = mapping(config, "SUBSCRIPTION_TENANT_MAP")
    if config.get("ACTIVATE_ROUTING") not in {"true", "false"}:
        raise ValueError("ACTIVATE_ROUTING must be an explicit boolean")
    if config.get("ACTIVATE_ROUTING") == "true":
        if config.get("ROUTING_CONFIGURATION_APPROVED") != "true":
            raise ValueError("Routing activation requires reviewed tenant migration and subscriptions")
        required(config, "ROUTING_APPROVAL_REFERENCE")
        image = required(config, "ROLLBACK_WORKER_IMAGE")
        if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?/incident-worker@sha256:[0-9a-f]{64}", image):
            raise ValueError("ROLLBACK_WORKER_IMAGE must be an immutable pre-routing Worker digest")
    # The dashboard is only redeployed on request. "keep" (or absent) leaves
    # the running dashboard exactly as it is, so a merge cannot lock anyone out.
    release = config.get("DASHBOARD_RELEASE") or "keep"
    if release not in {"keep", "signin-test", "promote"}:
        raise ValueError("DASHBOARD_RELEASE must be keep, signin-test or promote")
    if release != "keep":
        directory_id(config, "DASHBOARD_AUTH_CLIENT_ID")
    if release == "promote":
        # Promotion only after a person signed in on the labelled test revision.
        required(config, "DASHBOARD_SIGNIN_REFERENCE")
    if inputs_only:
        return
    for name in ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"]:
        directory_id(config, name)
    for name in ["RESOURCE_GROUP", "AUTH_AUDIENCE"]:
        required(config, name)
    mapping(config, "TENANT_MAP")
    if config["AZURE_CLIENT_ID"] not in managers:
        raise ValueError("The deploy identity client id must have an approved subscription-manager tenant mapping for authenticated read verification")
    if not re.fullmatch(r"-?[0-9]{1,20}", required(config, "TELEGRAM_CHAT_ID")):
        raise ValueError("Existing TELEGRAM_CHAT_ID must be configured")
    secret_uri = urlparse(required(config, "TELEGRAM_BOT_TOKEN_SECRET_URI"))
    if secret_uri.scheme != "https" or not secret_uri.hostname or not secret_uri.path.startswith("/secrets/"):
        raise ValueError("Existing Telegram Key Vault secret reference must be HTTPS")


if __name__ == "__main__":
    try:
        validate(os.environ, inputs_only="--inputs-only" in sys.argv[1:])
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1)
    print("Required approvals and configuration are present; values were not logged")
