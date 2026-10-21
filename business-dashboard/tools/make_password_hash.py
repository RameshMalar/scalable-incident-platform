"""Create or extend the local sign-in file (accounts with scrypt hashes and roles) for DASHBOARD_AUTH_MODE=local.

    Deployable (strong password required, prompted without echo):
        python tools/make_password_hash.py --username admin --role admin --out dashboard-local-auth.toml

    Add a second account to the same file:
        python tools/make_password_hash.py --username soc-viewer --role viewer --append --out dashboard-local-auth.toml

    Deployable with a generated password (written ONLY to the file you name, for
    you to move into a password manager and then delete; never printed):
        python tools/make_password_hash.py --generate admin-password.txt --out dashboard-local-auth.toml

    Local testing only (weak password allowed; the file is marked test-only and
    the container start-up check refuses it, so it can never be deployed):
        python tools/make_password_hash.py --local-testing --out .streamlit/local-auth.toml

The password itself is never printed, stored or logged; only its hash is written.
Upload a deployable file to Key Vault with `az keyvault secret set --file`.
"""
import argparse
import getpass
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_auth import ADMIN, ROLES, USERNAME, LocalAuthUnavailable, hash_password, load_credentials  # noqa: E402


def strong(password):
    classes = sum(any(c in group for c in password) for group in
                  (string.ascii_lowercase, string.ascii_uppercase, string.digits, string.punctuation))
    return len(password) >= 16 and classes >= 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="file to write")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--role", choices=ROLES, default=ADMIN, help="admin or viewer (default: admin)")
    parser.add_argument("--append", action="store_true", help="add or replace this account in an existing file")
    parser.add_argument("--local-testing", action="store_true", help="allow a weak password; marks the file test-only")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    parser.add_argument("--generate", metavar="PASSWORD_FILE", help="generate a strong password and write it only to this file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)

    if not USERNAME.fullmatch(args.username):
        parser.error("username may contain only letters, digits, '.', '_' and '-'")
    out = Path(args.out)
    existing = []
    if args.append:
        try:
            current = load_credentials(str(out))
        except LocalAuthUnavailable:
            parser.error(f"{out} is missing or not a valid sign-in file")
        if current.testing_only != args.local_testing:
            parser.error("--local-testing must match the existing file")
        if args.username in current.accounts and not args.force:
            parser.error(f"{args.username} already exists; use --force to replace it")
        existing = [(a.username, a.password_hash, a.role) for a in current.accounts.values() if a.username != args.username]
    elif out.exists() and not args.force:
        parser.error(f"{out} exists; use --force to replace it, or --append to add an account")
    if args.generate:
        if args.local_testing or args.password_stdin:
            parser.error("--generate cannot be combined with --local-testing or --password-stdin")
        password_file = Path(args.generate)
        if password_file.exists() and not args.force:
            parser.error(f"{password_file} exists; use --force to replace it")
        # 24 characters from all four classes; regenerated until it qualifies.
        alphabet = string.ascii_letters + string.digits + "-_.!@#%^*+="
        password = ""
        while not strong(password):
            password = "".join(secrets.choice(alphabet) for _ in range(24))
        password_file.write_text(password + "\n", encoding="utf-8", newline="\n")
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            parser.error("passwords do not match")
    if not password:
        parser.error("empty password")
    if not args.local_testing and not strong(password):
        parser.error("use at least 16 characters with 3 of: lower, upper, digit, symbol "
                     "(or --local-testing for a test-only file)")

    out.parent.mkdir(parents=True, exist_ok=True)
    accounts = existing + [(args.username, hash_password(password), args.role)]
    out.write_text(
        "[local_auth]\n"
        f"local_testing_only = {'true' if args.local_testing else 'false'}\n"
        + "".join(f'\n[[local_auth.accounts]]\nusername = "{name}"\npassword_hash = "{digest}"\nrole = "{role}"\n'
                  for name, digest, role in accounts),
        encoding="utf-8", newline="\n")
    print(f"wrote {out} ({'TEST-ONLY, refused by the deployed container' if args.local_testing else 'deployable'}); "
          + (f"the generated password is only in {args.generate} and was not printed" if args.generate
             else "the password was not printed or stored"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
