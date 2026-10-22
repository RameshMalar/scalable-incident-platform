"""Username/password sign-in for the dashboard (DASHBOARD_AUTH_MODE=local).

A demonstration fallback while Microsoft Entra sign-in is pending; Entra stays
the production design and its code is untouched.

  * Credentials come from a TOML file named by DASHBOARD_LOCAL_AUTH_FILE (in
    Azure a Key Vault secret mounted as a file), holding only usernames, scrypt
    hashes and roles -- never a plaintext password. Nothing is hard-coded.
  * Two roles: "admin" and "viewer". The single-account file is the team's
    administrator sign-in (this mode's original design), so it is an admin
    unless it says `role = "viewer"`. A multi-account file must name every role.
  * The role is read from the file on every request, never from the session,
    so removing an account or downgrading it takes effect immediately.
  * Verification is constant time, including for unknown usernames. Sessions
    live in Streamlit's server-side session state, expire after 30 idle
    minutes or 8 hours, and end on sign out.
  * Failed attempts are limited per client address and overall.

This mode carries no Entra token, so central access, Telegram onboarding and
the Entra simulator path stay disabled.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
import tomllib
from pathlib import Path
from typing import NamedTuple

import streamlit as st

logger = logging.getLogger("dashboard.local_auth")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
FAILURES_PER_CLIENT, FAILURES_TOTAL, WINDOW_SECONDS = 5, 30, 15 * 60
FAILURE_DELAY_SECONDS = 1.0
SESSION_KEY = "_local_auth_session"
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Features that act for a person need a genuine Entra token. Local sign-in has
# none, so the dashboard refuses to run with any of them switched on.
ENTRA_ONLY_FLAGS = ("ENABLE_CENTRAL_ACCESS", "ENABLE_TELEGRAM_ONBOARDING", "ENABLE_EVENT_SIMULATOR", "SIMULATOR_AUTH_VERIFIED")
ADMIN, VIEWER = "admin", "viewer"
ROLES = (ADMIN, VIEWER)
MAX_ACCOUNTS = 20
ACADEMIC_TITLE = "Scalable and Secure Incident Monitoring and Management Platform for AI Video Analytics"


class LocalAuthUnavailable(Exception):
    """Sign-in configuration is missing or invalid; fail closed."""


class LocalAccount(NamedTuple):
    username: str
    password_hash: str
    role: str


class LocalCredentials(NamedTuple):
    accounts: dict  # username -> LocalAccount
    testing_only: bool


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def hash_password(password, *, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def parse_hash(stored):
    """Return (n, r, p, salt, digest) or raise ValueError for any other shape."""
    parts = stored.split("$") if isinstance(stored, str) else []
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not a scrypt hash")
    n, r, p = (int(part) for part in parts[1:4])
    salt, digest = base64.b64decode(parts[4], validate=True), base64.b64decode(parts[5], validate=True)
    if n < 2 ** 14 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16 or len(salt) < 16 or len(digest) != 32:
        raise ValueError("weak or malformed scrypt parameters")
    return n, r, p, salt, digest


def verify_password(stored, password):
    try:
        n, r, p, salt, expected = parse_hash(stored)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(actual, expected)


def _account(entry, default_role):
    if not isinstance(entry, dict) or "password" in entry:
        raise ValueError("invalid account")
    username, password_hash = entry["username"], entry["password_hash"]
    role = entry.get("role", default_role)
    if not isinstance(username, str) or not USERNAME.fullmatch(username) or role not in ROLES:
        raise ValueError("invalid account")
    parse_hash(password_hash)
    return LocalAccount(username, password_hash, role)


def load_credentials(path=None):
    """Read the sign-in file: a list of accounts, or the legacy single account.

        [local_auth]                      [local_auth]
        local_testing_only = false        username = "admin"
        [[local_auth.accounts]]           password_hash = "scrypt$..."
        username = "admin"                (role defaults to "admin")
        password_hash = "scrypt$..."
        role = "admin"

    Any malformed account, unknown role or duplicate username rejects the whole
    file rather than skipping the bad entry.
    """
    path = path or os.environ.get("DASHBOARD_LOCAL_AUTH_FILE", "")
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8")) if path else None
        section = data["local_auth"]
        if "password" in section:
            raise ValueError("plaintext password")
        if "accounts" in section:
            if "username" in section or "password_hash" in section:
                raise ValueError("mixed formats")
            entries = section["accounts"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ACCOUNTS:
                raise ValueError("invalid accounts")
            accounts = [_account(entry, None) for entry in entries]
        else:
            accounts = [_account(section, ADMIN)]
        by_name = {account.username: account for account in accounts}
        if len(by_name) != len(accounts):
            raise ValueError("duplicate username")
        return LocalCredentials(by_name, bool(section.get("local_testing_only", False)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise LocalAuthUnavailable("local sign-in is not configured") from None


_DUMMY_HASH = []


def _dummy_hash():
    # Verified against when the username is unknown, so that path costs one
    # scrypt like every other and timing does not reveal valid usernames.
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_urlsafe(16)))
    return _DUMMY_HASH[0]


class AttemptLimiter:
    """Failed attempts per client and overall, in this process (one replica)."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.failures = clock, threading.Lock(), {}

    def _recent(self, key, now):
        recent = [t for t in self.failures.get(key, []) if t > now - WINDOW_SECONDS]
        self.failures[key] = recent
        return recent

    def blocked(self, client):
        with self.lock:
            now = self.clock()
            return len(self._recent(client, now)) >= FAILURES_PER_CLIENT or len(self._recent("*", now)) >= FAILURES_TOTAL

    def failed(self, client):
        with self.lock:
            now = self.clock()
            for key in (client, "*"):
                self._recent(key, now).append(now)

    def succeeded(self, client):
        with self.lock:
            self.failures.pop(client, None)


LIMITER = AttemptLimiter()


def client_address(headers):
    """Client address from the platform's X-Forwarded-For (last hop is added by
    the Container Apps ingress); "local" when running outside it."""
    try:
        values = {str(k).lower(): v for k, v in headers.items()}
    except Exception:
        return "local"
    forwarded = values.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or "local"


def entra_only_flags_enabled(env=os.environ):
    return [name for name in ENTRA_ONLY_FLAGS if env.get(name, "").lower() == "true"]


def current_session(now=None):
    """The signed-in username, or None. Expired sessions are cleared here."""
    session = st.session_state.get(SESSION_KEY)
    now = time.time() if now is None else now
    if not isinstance(session, dict):
        return None
    if now - session["signed_in_at"] > ABSOLUTE_SECONDS or now - session["last_seen"] > IDLE_SECONDS:
        st.session_state.pop(SESSION_KEY, None)
        return None
    session["last_seen"] = now
    return session["username"]


def sign_out():
    st.session_state.pop(SESSION_KEY, None)


def attempt_sign_in(username, password, client, credentials=None, limiter=None):
    """True on success. Never logs or returns the password."""
    limiter = limiter or LIMITER
    if limiter.blocked(client):
        logger.warning("local sign-in blocked by rate limit client=%s", client)
        return False
    accounts = (credentials or load_credentials()).accounts
    account = accounts.get(username) if isinstance(username, str) else None
    # Always run the hash so a wrong username takes as long as a wrong password.
    password_ok = verify_password(account.password_hash if account else _dummy_hash(),
                                  password if isinstance(password, str) else "")
    if account is not None and password_ok:
        limiter.succeeded(client)
        now = time.time()
        st.session_state[SESSION_KEY] = {"username": account.username, "signed_in_at": now, "last_seen": now}
        logger.info("local sign-in succeeded client=%s role=%s", client, account.role)
        return True
    limiter.failed(client)
    logger.warning("local sign-in failed client=%s", client)
    time.sleep(FAILURE_DELAY_SECONDS)
    return False


def require_signed_in(headers, banner=None):
    """Return the signed-in LocalAccount, or draw the sign-in form and stop.

    Nothing after this call runs for an unauthenticated session, so no page
    renders and no data is loaded before sign-in. The account, and so the
    role, is looked up in the credentials file on every call.
    """
    if entra_only_flags_enabled():
        st.error("Local sign-in cannot be combined with Entra-only features. Contact the administrator.")
        st.stop()
    try:
        credentials = load_credentials()
    except LocalAuthUnavailable:
        st.subheader(ACADEMIC_TITLE)
        st.error("Sign-in is not configured. Access is denied.")
        st.stop()
    username = current_session()
    if username:
        account = credentials.accounts.get(username)
        if account is not None:
            return account
        # The account was removed or renamed since sign-in.
        logger.warning("local session ended: account no longer configured")
        sign_out()
    if banner:
        _, middle, _ = st.columns([1, 2, 1])
        with middle:
            st.image(banner, width="stretch")
    st.subheader(ACADEMIC_TITLE)
    st.caption("Security Operations Center · sign in")
    client = client_address(headers)
    with st.form("local_sign_in", clear_on_submit=True):
        entered_user = st.text_input("Username", autocomplete="username")
        entered_password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if LIMITER.blocked(client):
            st.error("Too many failed attempts. Try again in 15 minutes.")
        elif attempt_sign_in(entered_user, entered_password, client, credentials):
            st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.stop()
