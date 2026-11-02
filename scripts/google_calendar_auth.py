#!/usr/bin/env python3
"""One-time loopback OAuth setup for the optional Google Calendar channel."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def load_client_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    config = config.get("installed") or config.get("web") or config
    if not config.get("client_id") or not config.get("client_secret"):
        raise SystemExit("OAuth client JSON must contain client_id and client_secret.")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("secrets/google-calendar.json")
    )
    parser.add_argument("--force", action="store_true", help="replace an existing token file")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {args.output}; pass --force to reconnect.")

    config = load_client_config(args.client_secret_file)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge = challenge.rstrip(b"=").decode()
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [None])[0] != state:
                self.send_error(400, "OAuth state did not match")
                callback["error"] = "OAuth state did not match"
                return
            callback["code"] = query.get("code", [None])[0]
            callback["error"] = query.get("error", [None])[0]
            self.send_response(200 if callback["code"] else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Calendar authorization received. You may close this browser tab."
                if callback["code"]
                else b"Calendar authorization was not completed."
            )

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 300
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorization_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening Google authorization in your browser. If needed, open this URL:\n")
    print(authorization_url)
    webbrowser.open(authorization_url)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if callback.get("error") or not callback.get("code"):
        raise SystemExit("Google Calendar authorization was not completed.")

    token_request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(
            {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "code": callback["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_request, timeout=30) as response:
            token = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit("Google token exchange failed; no credentials were written.") from exc
    if not token.get("refresh_token"):
        raise SystemExit(
            "Google did not return a refresh token. Revoke the app's access and retry with --force."
        )

    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    contents = {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": token["refresh_token"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = args.output.with_name(
        f".{args.output.name}.{secrets.token_hex(6)}.tmp"
    )
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(contents, indent=2) + "\n")
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Connected. Private refresh credentials were written to {args.output} (mode 0600).")
    print("Do not commit or share this file.")


if __name__ == "__main__":
    main()
