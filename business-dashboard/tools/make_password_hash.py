"""Create the local sign-in file (username + scrypt hash) for DASHBOARD_AUTH_MODE=local.

    Deployable (strong password required, prompted without echo):
        python tools/make_password_hash.py --out dashboard-local-auth.toml

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
from local_auth import USERNAME, hash_password  # noqa: E402


def strong(password):
    classes = sum(any(c in group for c in password) for group in
                  (string.ascii_lowercase, string.ascii_uppercase, string.digits, string.punctuation))
    return len(password) >= 16 and classes >= 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="file to write")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--local-testing", action="store_true", help="allow a weak password; marks the file test-only")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    parser.add_argument("--generate", metavar="PASSWORD_FILE", help="generate a strong password and write it only to this file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)

    if not USERNAME.fullmatch(args.username):
        parser.error("username may contain only letters, digits, '.', '_' and '-'")
    out = Path(args.out)
    if out.exists() and not args.force:
        parser.error(f"{out} exists; use --force to replace it")
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
    out.write_text(
        "[local_auth]\n"
        f'username = "{args.username}"\n'
        f'password_hash = "{hash_password(password)}"\n'
        f"local_testing_only = {'true' if args.local_testing else 'false'}\n",
        encoding="utf-8", newline="\n")
    print(f"wrote {out} ({'TEST-ONLY, refused by the deployed container' if args.local_testing else 'deployable'}); "
          + (f"the generated password is only in {args.generate} and was not printed" if args.generate
             else "the password was not printed or stored"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
