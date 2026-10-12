"""Create (or update) a sign-in user and give them a role. Run this yourself - it asks for a password.

    python scripts/create_user.py --project <project-id> --email you@example.com --role supervisor

The role (investigator or supervisor) is stored as a custom claim on the account; the API reads it from
the signed token, so it cannot be changed from the browser. After changing a role the user must sign out
and in again to get a fresh token. Uses the signed-in gcloud account for permission.
"""

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROLES = ("investigator", "supervisor")


def gcloud_token() -> str:
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        sys.exit("gcloud was not found on PATH.")
    return subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()


def call(project: str, token: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/projects/{project}/{path}",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "x-goog-user-project": project},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        message = json.load(error).get("error", {}).get("message", "")
        raise SystemExit(f"{path}: {error.code} {message}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", required=True, choices=ROLES)
    args = parser.parse_args()

    token = gcloud_token()
    password = getpass.getpass(f"Password for {args.email} (at least 6 characters; ignored if the user already exists): ")
    try:
        created = call(args.project, token, "accounts", {"email": args.email, "password": password, "emailVerified": True})
        uid = created["localId"]
        print(f"created user {args.email}")
    except SystemExit as exc:
        if "EMAIL_EXISTS" not in str(exc):
            raise
        found = call(args.project, token, "accounts:lookup", {"email": [args.email]})
        uid = found["users"][0]["localId"]
        print(f"user {args.email} already exists")
    call(args.project, token, "accounts:update", {"localId": uid, "customAttributes": json.dumps({"role": args.role})})
    print(f"role set to {args.role}")


if __name__ == "__main__":
    main()
