"""Refuse to start the dashboard with an unusable Microsoft sign-in setup.

Run before Streamlit in the container (see Dockerfile). A revision whose
sign-in configuration is missing or inconsistent never becomes healthy, so a
labelled sign-in-test revision fails in isolation while production traffic
stays on the previous revision -- instead of serving a page nobody can pass.

Prints only the names of failed checks, never a value from the secrets file.
Exit 0: usable. Exit 1: not usable.

Inputs (all non-secret, from the Container App):
  DASHBOARD_AUTH_MODE            oidc (default) | easyauth | local
  DASHBOARD_LOCAL_AUTH_FILE      local mode: mounted TOML with username + scrypt hash
  DASHBOARD_AUTH_CLIENT_ID       login app registration client ID
  DASHBOARD_EXPECTED_REDIRECT_URI  the exact callback this revision serves
  STREAMLIT_SECRETS_FILES        mounted secrets.toml (Key Vault reference)
  ENABLE_CENTRAL_ACCESS          when true, the API scope must be requested
  DASHBOARD_API_SCOPE            e.g. api://<api app id>/user_impersonation
"""
import json
import os
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

POLICY = Path(__file__).with_name("access-policy.json")


def _uuid(value):
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False


def check(env=os.environ):
    """Return the names of failed checks; empty means ready."""
    mode = env.get("DASHBOARD_AUTH_MODE", "oidc")
    if mode == "easyauth":
        return []
    if mode == "local":
        return check_local(env)
    if mode != "oidc":
        return ["auth_mode"]
    failures = []
    client_id = env.get("DASHBOARD_AUTH_CLIENT_ID", "")
    if not _uuid(client_id):
        failures.append("client_id_setting")
    expected_redirect = env.get("DASHBOARD_EXPECTED_REDIRECT_URI", "")
    parsed = urlparse(expected_redirect)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path != "/oauth2callback":
        failures.append("expected_redirect_setting")
    paths = [part for part in env.get("STREAMLIT_SECRETS_FILES", "").split(",") if part.strip()]
    if len(paths) != 1:
        return failures + ["secrets_file_setting"]
    try:
        secrets = tomllib.loads(Path(paths[0].strip()).read_text(encoding="utf-8"))
        directory = json.loads(POLICY.read_text(encoding="utf-8"))["directory_id"]
    except (OSError, ValueError, KeyError):
        return failures + ["secrets_file_readable"]
    auth = secrets.get("auth")
    provider = auth.get("microsoft") if isinstance(auth, dict) else None
    if not isinstance(auth, dict) or not isinstance(provider, dict):
        return failures + ["auth_sections"]
    if auth.get("redirect_uri") != expected_redirect:
        failures.append("redirect_uri_matches_revision")
    cookie = auth.get("cookie_secret")
    if not isinstance(cookie, str) or len(cookie) < 32:
        failures.append("cookie_secret_strength")
    if not {"id", "access"}.issubset(set(auth.get("expose_tokens") or [])):
        failures.append("expose_tokens")
    if provider.get("client_id") != client_id:
        failures.append("client_id_matches_setting")
    if not isinstance(provider.get("client_secret"), str) or not provider["client_secret"].strip():
        failures.append("client_secret_present")
    if provider.get("server_metadata_url") != f"https://login.microsoftonline.com/{directory}/v2.0/.well-known/openid-configuration":
        failures.append("approved_directory_metadata")
    kwargs = provider.get("client_kwargs")
    scopes = (kwargs.get("scope") or "").split() if isinstance(kwargs, dict) else []
    if "openid" not in scopes:
        failures.append("openid_scope")
    if env.get("ENABLE_CENTRAL_ACCESS", "").lower() == "true" and env.get("DASHBOARD_API_SCOPE", "") not in scopes:
        failures.append("api_scope_for_central_access")
    return failures


# Never accepted in a deployed container, whatever the hash says about itself.
WEAK_PASSWORDS = ("admin123!", "admin123", "admin", "password", "Password1!", "changeme", "123456", "letmein")


def check_local(env):
    """Local sign-in: a strong, deployable hash and no Entra-only features."""
    import local_auth  # only needed in this mode

    failures = [f"entra_only_feature_enabled:{name}" for name in local_auth.entra_only_flags_enabled(env)]
    try:
        username, password_hash, testing_only = local_auth.load_credentials(env.get("DASHBOARD_LOCAL_AUTH_FILE", ""))
    except local_auth.LocalAuthUnavailable:
        return failures + ["local_auth_file"]
    if testing_only:
        failures.append("local_auth_testing_only_hash")
    if any(local_auth.verify_password(password_hash, weak) for weak in WEAK_PASSWORDS):
        failures.append("local_auth_weak_password")
    return failures


def main():
    failures = check()
    if failures:
        print("dashboard auth preflight FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("dashboard auth preflight passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
