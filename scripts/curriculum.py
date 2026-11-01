#!/usr/bin/env python3
"""Small operator client for the authenticated curriculum API."""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(path: str, method: str = "GET", body=None, idempotency_key=None):
    base = os.environ["ADMIN_API_URL"].rstrip("/")
    token = os.environ["ADMIN_SERVICE_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if (
        method == "POST"
        and path.startswith("/v1/admin/")
        and not path.endswith("/validate")
    ):
        import uuid

        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
    request = Request(base + path, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urlopen(request) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code >= 500 and attempt < 2:
                continue
            sys.stderr.write(error.read().decode() + "\n")
            raise SystemExit(error.code)
        except URLError:
            if attempt == 2:
                raise


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    curriculum = commands.add_parser("curriculum")
    actions = curriculum.add_subparsers(dest="action", required=True)
    imp = actions.add_parser("import")
    imp.add_argument("file")
    val = actions.add_parser("validate")
    val.add_argument("version")
    preview = actions.add_parser("preview")
    preview.add_argument("version")
    preview.add_argument("--mapping")
    activate = actions.add_parser("activate")
    activate.add_argument("confirmation_token")
    args = parser.parse_args()
    if args.action == "import":
        with open(args.file, encoding="utf-8") as source:
            result = call("/v1/admin/curricula/import", "POST", json.load(source))
    elif args.action == "validate":
        result = call(f"/v1/admin/curricula/{args.version}/validate", "POST", {})
    elif args.action == "preview":
        mapping = []
        if args.mapping:
            with open(args.mapping, encoding="utf-8") as source:
                mapping = json.load(source)
        result = call(
            f"/v1/admin/curricula/{args.version}/preview", "POST", {"mapping": mapping}
        )
    else:
        result = call(f"/v1/admin/confirmations/{args.confirmation_token}", "POST", {})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
