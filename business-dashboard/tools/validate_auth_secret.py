"""Check a dashboard sign-in secrets.toml locally, before it goes into Key Vault.

    python validate_auth_secret.py path/to/secrets.toml

Prints only PASS / WARN / FAIL lines and safe error categories. It never prints
the file's contents or any value from it (client secret, cookie secret, IDs,
URLs). It only reads the file: it uploads nothing and changes nothing.

Python 3.11+ (standard library only). Exit code 0 = ready to upload, 1 = fix first.
"""
import sys
import tomllib
from pathlib import Path

EXPECTED_REDIRECT = "https://incident-dashboard---signin-test.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/oauth2callback"
EXPECTED_CLIENT_ID = "fbbe63e1-1f8f-4d4d-b695-0f9982842081"
EXPECTED_METADATA = "https://login.microsoftonline.com/5ba5ef5e-3109-4e77-85bd-cfeb0d347e82/v2.0/.well-known/openid-configuration"
API_SCOPE = "api://fbbe63e1-1f8f-4d4d-b695-0f9982842081/user_impersonation"
CURLY_QUOTES = "“”‘’"

results = []


def report(status, check, detail=""):
    results.append(status)
    print(f"{status:<4}  {check}" + (f"  ({detail})" if detail else ""))


def main(argv):
    if sys.version_info < (3, 11):
        print("FAIL  Python 3.11 or newer is required")
        return 1
    if len(argv) != 2:
        print("usage: python validate_auth_secret.py path/to/secrets.toml")
        return 1
    path = Path(argv[1])

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        report("FAIL", "file exists", "file not found")
        return 1
    except IsADirectoryError:
        report("FAIL", "file exists", "path is a folder, not a file")
        return 1
    except OSError as error:
        report("FAIL", "file readable", type(error).__name__)
        return 1
    report("PASS" if raw else "FAIL", "file is not empty")
    if not raw:
        return 1

    if raw.startswith(b"\xef\xbb\xbf"):
        report("FAIL", "no UTF-8 byte-order mark", "file starts with a BOM; save as UTF-8 without BOM")
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        report("FAIL", "UTF-8 encoding", "file is UTF-16; save as UTF-8 without BOM")
        return 1
    else:
        report("PASS", "no byte-order mark")
    try:
        text = raw.decode("utf-8")
        report("PASS", "UTF-8 encoding")
    except UnicodeDecodeError as error:
        report("FAIL", "UTF-8 encoding", f"invalid byte at offset {error.start}")
        return 1

    lines = text.count("\n") + 1
    report("PASS" if lines > 1 or text.lstrip("﻿").startswith("auth = {") else "WARN", "line breaks present",
           f"{lines} line(s)" + ("; single-line is fine only in the inline-table form" if lines == 1 else ""))
    if "\\n" in text:
        report("WARN", "no literal \\n text", "found the characters \\n; real line breaks are needed instead")
    curly = [(n + 1, line.index(q) + 1) for n, line in enumerate(text.splitlines()) for q in CURLY_QUOTES if q in line]
    if curly:
        report("FAIL", "straight quotes only", f"curly quote at line {curly[0][0]}, column {curly[0][1]}")

    try:
        data = tomllib.loads(text.lstrip("﻿") if raw.startswith(b"\xef\xbb\xbf") else text)
        if raw.startswith(b"\xef\xbb\xbf"):
            report("FAIL", "TOML parses as stored", "parses only after removing the BOM, which the dashboard does not do")
        else:
            report("PASS", "TOML parses")
    except tomllib.TOMLDecodeError as error:
        # tomllib messages give a reason and a position, never file content.
        report("FAIL", "TOML parses", str(error))
        return 1

    auth = data.get("auth")
    provider = auth.get("microsoft") if isinstance(auth, dict) else None
    report("PASS" if isinstance(auth, dict) else "FAIL", "[auth] section present")
    report("PASS" if isinstance(provider, dict) else "FAIL", "[auth.microsoft] section present")
    if not isinstance(auth, dict) or not isinstance(provider, dict):
        return 1

    report("PASS" if auth.get("redirect_uri") == EXPECTED_REDIRECT else "FAIL", "auth.redirect_uri is the signin-test callback")
    cookie = auth.get("cookie_secret")
    if not isinstance(cookie, str) or len(cookie) < 32:
        report("FAIL", "auth.cookie_secret length", "missing or shorter than 32 characters")
    elif len(cookie) < 64:
        report("WARN", "auth.cookie_secret length", "accepted, but 64+ characters is recommended")
    else:
        report("PASS", "auth.cookie_secret length", "64+ characters")
    tokens = auth.get("expose_tokens")
    report("PASS" if isinstance(tokens, list) and {"id", "access"} <= set(tokens) else "FAIL", 'auth.expose_tokens includes "id" and "access"')

    report("PASS" if provider.get("client_id") == EXPECTED_CLIENT_ID else "FAIL", "auth.microsoft.client_id is incident-platform-api")
    secret = provider.get("client_secret")
    report("PASS" if isinstance(secret, str) and secret.strip() and "<" not in secret else "FAIL",
           "auth.microsoft.client_secret present", "" if isinstance(secret, str) and secret.strip() and "<" not in secret
           else "missing, empty, or still the <placeholder>")
    report("PASS" if provider.get("server_metadata_url") == EXPECTED_METADATA else "FAIL", "auth.microsoft.server_metadata_url is the NUS directory")
    kwargs = provider.get("client_kwargs")
    scopes = (kwargs.get("scope") or "").split() if isinstance(kwargs, dict) else []
    report("PASS" if "openid" in scopes else "FAIL", 'client_kwargs.scope includes "openid"')
    if API_SCOPE in scopes:
        report("WARN", "client_kwargs.scope for the initial test", "API scope present; the initial test expects openid profile email only")

    failed = "FAIL" in results
    print("\nRESULT: " + ("FIX BEFORE UPLOADING" if failed else "READY TO UPLOAD") + "  (no values were printed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
