"""Minimal client for the local in-app Resolve bridge (protocol 1.0 of the installed davinci-resolve-mcp bridge).

Why a separate client instead of importing the MCP server's: the repository must run without `tools/`, and this
package needs only four bridge operations (`health`, `call`, `list_methods`, `release_handles`). The wire format was
read from `tools/davinci-resolve-mcp/src/utils/resolve_bridge_client.py` (v4.19.3): one JSON line per request,
HMAC-SHA256 over the canonical request (sorted keys, compact separators, ASCII) with the shared token from the
machine-local bridge config.

Safety rules kept here:

* The bridge config is only READ. Its token is held in memory, never logged, printed, returned or serialized
  (`repr` hides it). The config's host must be loopback.
* This module is transport only. It is used by `live.LiveResolveAdapter`, which exposes a fixed set of
  operations; nothing above the adapter receives a `Handle` or can name an arbitrary method.
* One TCP connection per request: simpler failure semantics than keep-alive (a connect failure always means
  "nothing sent").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import AdapterError, FailureClass

PROTOCOL_VERSION = "1.0"
MAX_LINE_BYTES = 1_048_576
ENV_CONFIG = "DAVINCI_RESOLVE_BRIDGE_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config" / "davinci-resolve-mcp" / "bridge.json"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class Handle:
    """An opaque reference to a live Resolve object held by the bridge."""

    id: str
    shape: Optional[str] = None


def config_path() -> Path:
    override = os.environ.get(ENV_CONFIG)
    return Path(override).expanduser() if override else DEFAULT_CONFIG


def canonical(payload: Dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sign(token: str, payload: Dict[str, Any]) -> str:
    return hmac.new(token.encode("utf-8"), canonical(payload), hashlib.sha256).hexdigest()


class BridgeClient:
    def __init__(self, host: str, port: int, token: str, *, timeout: float = 30.0,
                 allowed_media_roots: Tuple[str, ...] = (), allowed_output_roots: Tuple[str, ...] = ()) -> None:
        if host not in LOOPBACK:
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge host must be loopback", {"host": host})
        if not isinstance(token, str) or len(token) < 16:
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge config has no usable token")
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self._sign = lambda payload, _t=token: sign(_t, payload)   # the token lives only in this closure
        self.allowed_media_roots = tuple(allowed_media_roots)
        self.allowed_output_roots = tuple(allowed_output_roots)
        self.requests_sent = 0

    def __repr__(self) -> str:
        return f"BridgeClient({self.host}:{self.port}, token=<hidden>)"

    @classmethod
    def from_config(cls, path: Optional[Path] = None, *, timeout: float = 30.0) -> "BridgeClient":
        p = Path(path) if path else config_path()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AdapterError(FailureClass.BRIDGE_UNAVAILABLE, "bridge config not found; install the bridge first",
                               {"config": str(p)})
        except (OSError, ValueError) as exc:
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, f"bridge config unreadable: {type(exc).__name__}",
                               {"config": str(p)})
        if not isinstance(raw, dict):
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge config is not an object")

        def roots(key: str) -> Tuple[str, ...]:
            v = raw.get(key) or []
            return tuple(str(x) for x in v if isinstance(x, str))

        return cls(str(raw.get("host", "127.0.0.1")), int(raw.get("port", 0) or 0), raw.get("token"),
                   timeout=timeout, allowed_media_roots=roots("allowed_media_roots"),
                   allowed_output_roots=roots("allowed_output_roots"))

    # ── transport ──
    def request(self, operation: str, arguments: Dict[str, Any], *, timeout: Optional[float] = None) -> Any:
        payload = {"protocol": PROTOCOL_VERSION, "id": str(uuid.uuid4()), "timestamp": int(time.time()),
                   "nonce": secrets.token_urlsafe(24), "operation": operation, "arguments": arguments}
        payload["signature"] = self._sign(payload)
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            raise AdapterError(FailureClass.VALIDATION, "request exceeds the bridge size limit")
        t = self.timeout if timeout is None else float(timeout)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=min(t, 5.0))
        except OSError as exc:
            raise AdapterError(FailureClass.BRIDGE_UNAVAILABLE,
                               "cannot reach the in-Resolve bridge; start Workspace > Scripts > resolve_bridge",
                               {"error": type(exc).__name__, "port": self.port})
        self.requests_sent += 1
        try:
            with sock, sock.makefile("rb") as reader:
                sock.settimeout(t)
                sock.sendall(line)
                raw = reader.readline(MAX_LINE_BYTES + 1)
        except (TimeoutError, socket.timeout):
            raise AdapterError(FailureClass.BRIDGE_TIMEOUT, "Resolve did not answer in time (modal dialog?)",
                               {"operation": operation})
        except OSError as exc:
            raise AdapterError(FailureClass.BRIDGE_TIMEOUT, "connection lost after the request was sent",
                               {"operation": operation, "error": type(exc).__name__})
        if not raw:
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge closed without replying", {"operation": operation})
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge returned invalid JSON")
        if not isinstance(reply, dict) or reply.get("id") != payload["id"]:
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "bridge reply does not match the request")
        if reply.get("ok") is not True:
            err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            code = str(err.get("code", "bridge_error"))
            cls = FailureClass.BRIDGE_PROTOCOL if code in ("auth_failed", "bad_signature", "stale_request",
                                                             "replay", "protocol_mismatch", "stale_handle",
                                                             "unknown_operation") else FailureClass.RESOLVE_REFUSED
            raise AdapterError(cls, f"bridge refused {operation}: {code}",
                               {"code": code, "message": str(err.get("message", ""))[:300]})
        return reply.get("result")

    # ── object calls ──
    def _encode(self, v: Any) -> Any:
        if isinstance(v, Handle):
            return {"__handle__": v.id}
        if isinstance(v, dict):
            return {k: self._encode(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self._encode(x) for x in v]
        return v

    def _decode(self, v: Any) -> Any:
        if isinstance(v, dict):
            if v.get("__handle__") is not None:
                return Handle(str(v["__handle__"]), v.get("__shape__"))
            return {k: self._decode(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._decode(x) for x in v]
        return v

    def call(self, target: Any, method: str, *args: Any, timeout: Optional[float] = None) -> Any:
        handle = target.id if isinstance(target, Handle) else str(target)
        result = self.request("call", {"target": handle, "method": method, "args": [self._encode(a) for a in args]},
                              timeout=timeout)
        result = result or {}
        if isinstance(result, dict) and result.get("truncated"):
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, f"reply to {method} was truncated by the bridge",
                               {"truncated": result.get("truncated")})
        return self._decode(result.get("value") if isinstance(result, dict) else None)

    def methods(self, target: Any) -> List[str]:
        handle = target.id if isinstance(target, Handle) else str(target)
        result = self.request("list_methods", {"target": handle}) or {}
        return sorted(str(m) for m in (result.get("methods") or ()))

    def health(self) -> Dict[str, Any]:
        return self.request("health", {}, timeout=10.0) or {}

    def release_handles(self) -> None:
        try:
            self.request("release_handles", {})
        except AdapterError:
            pass
