"""Publish the `web/` folder to Firebase Hosting without the firebase CLI login (uses your gcloud login).

    python scripts/deploy_hosting.py --project <project-id>

Follows Firebase Hosting's REST flow: create a version (with the rewrites and headers from firebase.json),
tell it each file's SHA-256 (of the gzipped bytes), upload only what it asks for, finalize, then release.
"""

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://firebasehosting.googleapis.com/v1beta1"


def token() -> str:
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    return subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()


def call(url: str, tok: str, project: str, method: str = "POST", body=None, raw: bytes | None = None):
    headers = {"Authorization": f"Bearer {tok}", "x-goog-user-project": project}
    data = raw
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8")
        return json.loads(text) if text else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--site", help="defaults to the project id")
    args = parser.parse_args()
    site, tok = args.site or args.project, token()

    config = json.loads((ROOT / "firebase.json").read_text(encoding="utf-8"))["hosting"]
    public = ROOT / config["public"]
    # firebase.json says "source" and lists headers as {key, value}; the REST API says "glob" and wants a map.
    version_config = {
        "rewrites": [{"glob": r["source"], **{k: v for k, v in r.items() if k != "source"}}
                     for r in config.get("rewrites", [])],
        "headers": [{"glob": h["source"], "headers": {x["key"]: x["value"] for x in h["headers"]}}
                    for h in config.get("headers", [])],
    }

    version = call(f"{API}/sites/{site}/versions", tok, args.project, body={"config": version_config})["name"]

    files = {}  # "/path" -> (sha256 of gzipped bytes, gzipped bytes)
    for path in sorted(p for p in public.rglob("*") if p.is_file()):
        packed = gzip.compress(path.read_bytes(), mtime=0)
        files["/" + path.relative_to(public).as_posix()] = (hashlib.sha256(packed).hexdigest(), packed)

    populated = call(f"{API}/{version}:populateFiles", tok, args.project,
                     body={"files": {name: digest for name, (digest, _) in files.items()}})
    by_hash = {digest: packed for digest, packed in files.values()}
    for digest in populated.get("uploadRequiredHashes", []):
        call(f"{populated['uploadUrl']}/{digest}", tok, args.project, raw=by_hash[digest])

    call(f"{API}/{version}?update_mask=status", tok, args.project, method="PATCH", body={"status": "FINALIZED"})
    release = call(f"{API}/sites/{site}/releases?versionName={version}", tok, args.project, body={})
    print(f"released {len(files)} files: https://{site}.web.app  ({release.get('name', '')})")


if __name__ == "__main__":
    main()
