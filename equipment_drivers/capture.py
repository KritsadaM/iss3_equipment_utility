"""
Record every exchange the drivers have with a device, so replies from real
hardware can be kept and turned into test fixtures (tests/fixtures/).

    with CaptureSession("captures", label="192.168.1.40"):
        driver.connect(...); driver.get_status(1)

Each session writes a directory containing:
    exchanges.jsonl          one line per exchange: request, status, timing, body file
    NNN-<slug>.json|.txt     each response body / CLI reply, byte-for-byte as received

It hooks requests.Session.send (WTI, Raritan) and SshCliTransport.open/send
(APC) for the duration of the `with` block, so it sees discovery's identify
calls too. Credentials are never written: no Authorization headers, no
passwords, and user:pass is stripped from URLs.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from equipment_drivers.cli_transport import SshCliTransport


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "exchange"


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


class CaptureSession:
    def __init__(self, directory: str, label: str = ""):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(directory, f"{stamp}-{_slug(label)}" if label else stamp)
        self.path, n = base, 1
        while os.path.exists(self.path):  # several runs within the same second
            n += 1
            self.path = f"{base}-{n}"
        self.count = 0
        self._lock = threading.Lock()
        self._originals = {}

    def __enter__(self):
        os.makedirs(self.path, exist_ok=True)
        session_send = requests.Session.send
        ssh_open = SshCliTransport.open
        ssh_send = SshCliTransport.send
        self._originals = {"session_send": session_send, "ssh_open": ssh_open, "ssh_send": ssh_send}
        capture = self

        def send(session, request, **kwargs):
            started = time.monotonic()
            try:
                response = session_send(session, request, **kwargs)
            except Exception as e:
                capture._record_http(request, None, started, error=e)
                raise
            capture._record_http(request, response, started)
            return response

        def open_(transport, host, port, username, password, timeout=10.0):
            started = time.monotonic()
            request = {"host": host, "port": port, "username": username}
            try:
                ssh_open(transport, host, port, username, password, timeout=timeout)
            except Exception as e:
                capture._record("ssh-cli", "login", request, None, started, error=e)
                raise
            capture._record("ssh-cli", "login", request, transport.banner, started)

        def send_cli(transport, command):
            started = time.monotonic()
            try:
                reply = ssh_send(transport, command)
            except Exception as e:
                capture._record("ssh-cli", command, {"command": command}, None, started, error=e)
                raise
            capture._record("ssh-cli", command, {"command": command}, reply, started)
            return reply

        requests.Session.send = send
        SshCliTransport.open = open_
        SshCliTransport.send = send_cli
        return self

    def __exit__(self, exc_type, exc, tb):
        requests.Session.send = self._originals["session_send"]
        SshCliTransport.open = self._originals["ssh_open"]
        SshCliTransport.send = self._originals["ssh_send"]
        return False

    def _record_http(self, request, response, started, error: Optional[Exception] = None):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        req = {"method": request.method, "url": _strip_userinfo(request.url), "body": body}
        slug = f"{request.method} {urlsplit(request.url).path}"
        try:
            rpc_method = json.loads(body).get("method") if body else None
        except (ValueError, AttributeError):
            rpc_method = None
        if rpc_method:  # Raritan JSON-RPC: every call is a POST, the method tells them apart
            slug += f" {rpc_method}"
        if response is None:
            self._record("http", slug, req, None, started, error=error)
        else:
            self._record("http", slug, req, response.text, started, status=response.status_code,
                         content_type=response.headers.get("Content-Type"))

    def _record(self, protocol, slug, request, body, started, error=None, status=None, content_type=None):
        elapsed_ms = round((time.monotonic() - started) * 1000)
        with self._lock:
            self.count += 1
            entry = {"seq": self.count, "time": datetime.now().isoformat(timespec="milliseconds"),
                     "protocol": protocol, "request": request, "elapsed_ms": elapsed_ms}
            if status is not None:
                entry["status"] = status
            if content_type:
                entry["content_type"] = content_type
            if error is not None:
                entry["error"] = f"{type(error).__name__}: {error}"
            if body is not None:
                try:
                    json.loads(body)
                    ext = "json"
                except ValueError:
                    ext = "txt"
                name = f"{self.count:03d}-{_slug(slug)}.{ext}"
                with open(os.path.join(self.path, name), "w", encoding="utf-8", newline="") as f:
                    f.write(body)
                entry["body_file"] = name
            with open(os.path.join(self.path, "exchanges.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
