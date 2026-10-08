"""Reachability probes for the two video sources (MT11 RTSP, Android relay).

Both are short blocking network calls meant for a background thread; they
return "down" on every failure rather than raising, so the monitor loop stays
simple. Only a malformed URL raises (that is a configuration error). Every
probe is bounded by ``timeout_s`` in total, not per read.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

RTSP_DEFAULT_PORT = 554
RTSP_MAX_REPLY_BYTES = 4096
ANDROID_MAX_BODY_BYTES = 16 * 1024
USER_AGENT = "mt11-camera-ui"
# 401 means "server and path exist, credentials needed": MediaMTX holds those, we only check reachability.
RTSP_OK_STATUSES = frozenset(range(200, 300)) | {401}
FORBIDDEN_URL_CHARS = frozenset("\r\n\t ")


def rtsp_request_uri(url: str) -> tuple[str, int, str]:
    """Validate ``url`` and rebuild it from parsed parts so nothing unexpected reaches the wire."""
    if any(ch in FORBIDDEN_URL_CHARS for ch in url):
        raise ValueError("RTSP URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "rtsp" or not parsed.hostname:
        raise ValueError(f"not an rtsp:// URL: {url!r}")
    port = parsed.port or RTSP_DEFAULT_PORT
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return parsed.hostname, port, f"rtsp://{host}:{port}{path}{query}"


def probe_rtsp(url: str, timeout_s: float = 2.0) -> bool:
    """True when the RTSP server answers DESCRIBE for ``url`` (2xx, or 401 = exists but needs auth).

    DESCRIBE (rather than a bare TCP connect or OPTIONS) proves the stream path
    exists, which is what MediaMTX needs before it can relay it.
    """
    host, port, uri = rtsp_request_uri(url)
    request = (
        f"DESCRIBE {uri} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        f"User-Agent: {USER_AGENT}\r\n\r\n"
    ).encode()
    deadline = time.monotonic() + timeout_s
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            sock.sendall(request)
            reply = _read_status_line(sock, deadline)
    except (OSError, ValueError):
        return False
    return _rtsp_status_is_ok(reply)


def _read_status_line(sock: socket.socket, deadline: float) -> bytes:
    chunks = b""
    while b"\r\n" not in chunks and len(chunks) < RTSP_MAX_REPLY_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("RTSP status line did not arrive in time")
        sock.settimeout(remaining)
        chunk = sock.recv(1024)
        if not chunk:
            break
        chunks += chunk
    return chunks.split(b"\r\n", 1)[0]


def _rtsp_status_is_ok(status_line: bytes) -> bool:
    parts = status_line.decode(errors="replace").split()
    if len(parts) < 2 or not parts[0].startswith("RTSP/"):
        return False
    return parts[1].isdigit() and int(parts[1]) in RTSP_OK_STATUSES


def probe_android_status(url: str, timeout_s: float = 2.0) -> dict[str, Any] | None:
    """The Android relay's ``/status`` JSON object, or None when it cannot be read."""
    if not url:
        return None  # Android source disabled
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout_s) as res:
            if res.status != 200:
                return None
            payload = json.loads(res.read(ANDROID_MAX_BODY_BYTES))
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None
    return payload if isinstance(payload, dict) else None
