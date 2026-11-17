"""Read-only DaVinci Resolve discovery (task T-DEV-005).

Answers, without ever mutating anything: is Resolve running, is the in-app bridge listening on
127.0.0.1, does a read-only MCP call answer, which project/timeline is current, and does that match
the identity the machine-local config expects.

Every field is {"status": "OBSERVED"|"UNAVAILABLE", "value": ..., "reason": ...}. The verdict is
MATCH / MISMATCH / UNCONFIGURED / UNAVAILABLE and is never decided by project name alone.
All transports (process list, socket, MCP) are injectable; nothing here opens, loads, creates,
saves or changes a project, and nothing clicks in the UI. Stdlib only.

CLI:  python -B -m dev_supervisor.resolve_discovery [--config FILE] [--mcp-snapshot FILE]
Exit: 0 MATCH, 1 MISMATCH, 2 UNCONFIGURED, 3 UNAVAILABLE.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

OBSERVED = "OBSERVED"
UNAVAILABLE = "UNAVAILABLE"
MATCH, MISMATCH, UNCONFIGURED = "MATCH", "MISMATCH", "UNCONFIGURED"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 49632
PROCESS_NAMES = ("resolve.exe", "resolve")
CONFIG_ENV = "CREATORSTUDIO_RESOLVE_IDENTITY"
# The ONLY MCP calls this module may make. All are read-only.
READ_ONLY_CALLS = (
    ("resolve_control", "runtime_mode"),
    ("project_manager", "get_current"),
    ("project_manager", "list"),
    ("timeline", "get_name"),
)
EXIT_CODES = {MATCH: 0, MISMATCH: 1, UNCONFIGURED: 2, UNAVAILABLE: 3}

MANUAL_FALLBACK = (
    "Bridge start cannot be automated on Resolve Free: the user must do it inside Resolve. "
    "1) Open the intended project in Resolve (File > Project Manager). "
    "2) Workspace > Scripts > resolve_bridge. "
    "3) Leave that script running (it holds the listener on 127.0.0.1:49632 open). "
    "Then re-run discovery. Claude cannot click this menu."
)

Transport = Callable[[str, str], Any]


class IdentityMismatch(RuntimeError):
    """The open Resolve project is not the configured one: refuse to proceed."""


def _obs(value: Any) -> Dict[str, Any]:
    return {"status": OBSERVED, "value": value}


def _na(reason: str) -> Dict[str, Any]:
    return {"status": UNAVAILABLE, "value": None, "reason": reason}


# ---- default (real) transports; tests inject fakes ---------------------------------------------

def default_process_lister() -> List[str]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15)
    names = []
    for line in out.stdout.splitlines():
        parts = line.strip().strip('"').split('","')
        if parts and parts[0]:
            names.append(parts[0])
    return names


def default_socket_factory(host: str, port: int, timeout: float):
    return socket.create_connection((host, port), timeout=timeout)


def _unwrap(resp: Any) -> Any:
    if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
        return resp["result"]
    return resp


def snapshot_transport(snapshot: Dict[str, Any]) -> Transport:
    """Transport answering from a JSON snapshot of read-only MCP replies (made by an agent)."""
    table = {
        ("resolve_control", "runtime_mode"): snapshot.get("runtime_mode"),
        ("project_manager", "get_current"): snapshot.get("current_project"),
        ("project_manager", "list"): ({"projects": snapshot["projects"]} if "projects" in snapshot else None),
        ("timeline", "get_name"): snapshot.get("timeline"),
    }

    def call(tool: str, action: str) -> Any:
        val = table.get((tool, action))
        if val is None:
            raise LookupError("not in snapshot")
        return val
    return call


# ---- probes ------------------------------------------------------------------------------------

def check_running(lister: Callable[[], List[str]]) -> Dict[str, Any]:
    try:
        names = [n.lower() for n in lister()]
    except Exception as exc:  # noqa: BLE001 - any lister fault is reported, not raised
        return _na("process list failed: %s" % type(exc).__name__)
    return _obs(any(n in PROCESS_NAMES for n in names))


def check_bridge(factory: Callable[..., Any], host: str = BRIDGE_HOST, port: int = BRIDGE_PORT,
                 timeout: float = 2.0) -> Dict[str, Any]:
    """TCP connect only, loopback only. No bytes are sent (the bridge protocol is authenticated)."""
    if host != BRIDGE_HOST:
        raise ValueError("bridge probe is loopback only")
    try:
        sock = factory(host, port, timeout)
    except (ConnectionRefusedError, TimeoutError, socket.timeout):
        return _obs(False)
    except OSError as exc:
        return _na("connect failed: %s" % type(exc).__name__)
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass
    return _obs(True)


def _read(transport: Optional[Transport], tool: str, action: str):
    if (tool, action) not in READ_ONLY_CALLS:
        raise PermissionError("not a whitelisted read-only call")
    if transport is None:
        return None, "no MCP transport supplied"
    try:
        return _unwrap(transport(tool, action)), None
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:80])


def discover(process_lister=default_process_lister, socket_factory=default_socket_factory,
             mcp: Optional[Transport] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    report: Dict[str, Any] = {"read_only": True, "mcp_calls": [list(c) for c in READ_ONLY_CALLS]}
    report["resolve_running"] = check_running(process_lister)
    report["bridge_listening"] = check_bridge(socket_factory)

    mode, err = _read(mcp, "resolve_control", "runtime_mode")
    database = None
    if isinstance(mode, dict) and isinstance(mode.get("database"), dict):
        database = mode["database"].get("DbName")
    report["mcp_answers"] = _obs(True) if mode is not None else _na(err or "empty reply")
    report["database"] = _obs(database) if database else _na(err or "database not exposed")
    report["folder"] = _na("project folder is not exposed by the read-only calls used")

    cur, err = _read(mcp, "project_manager", "get_current")
    if isinstance(cur, dict) and cur.get("name"):
        report["project_name"] = _obs(cur["name"])
        report["project_id"] = _obs(cur["id"]) if cur.get("id") else _na("API exposed no unique id")
    else:
        report["project_name"] = _na(err or "no current project")
        report["project_id"] = _na(err or "no current project")

    tl, err = _read(mcp, "timeline", "get_name")
    if isinstance(tl, dict) and tl.get("name"):
        report["timeline_name"] = _obs(tl["name"])
    else:
        report["timeline_name"] = _na(err or "no current timeline")

    lst, err = _read(mcp, "project_manager", "list")
    if isinstance(lst, dict) and isinstance(lst.get("projects"), list):
        report["project_list"] = _obs(list(lst["projects"]))
    else:
        report["project_list"] = _na(err or "no project list")

    if report["project_id"]["status"] == OBSERVED:
        report["identity_basis"] = "unique_id"
    elif report["project_name"]["status"] == OBSERVED:
        report["identity_basis"] = "name+database(+folder unavailable)"
    else:
        report["identity_basis"] = None
    report["identity"] = compare_identity(report, config)
    report["verdict"] = report["identity"]["verdict"]
    if report["bridge_listening"].get("value") is not True or report["verdict"] == UNAVAILABLE:
        report["manual_fallback"] = MANUAL_FALLBACK
    return report


# ---- identity ----------------------------------------------------------------------------------

def load_config(path: Optional[str] = None, environ=None) -> Optional[Dict[str, Any]]:
    """Machine-local config; None when absent. Raises ValueError on an unreadable file."""
    environ = os.environ if environ is None else environ
    p = path or environ.get(CONFIG_ENV)
    if not p:
        return None
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("identity config unreadable: %s" % type(exc).__name__)
    if not isinstance(data, dict):
        raise ValueError("identity config must be a JSON object")
    return data


def compare_identity(report: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Never decides by name alone: needs project_id, or project_name together with database."""
    if not config:
        return {"verdict": UNCONFIGURED, "reason": "no machine-local identity config"}
    exp_id, exp_name, exp_db = config.get("project_id"), config.get("project_name"), config.get("database")
    if not exp_id and not (exp_name and exp_db):
        return {"verdict": UNCONFIGURED, "reason": "config needs project_id, or project_name plus database"}
    name, pid, db = report["project_name"], report["project_id"], report["database"]
    if name["status"] != OBSERVED:
        return {"verdict": UNAVAILABLE, "reason": "current project not observed"}
    if exp_id:
        if pid["status"] != OBSERVED:
            return {"verdict": UNAVAILABLE, "reason": "unique id not observed; refusing to fall back to name"}
        return {"verdict": MATCH if pid["value"] == exp_id else MISMATCH, "compared": "unique_id"}
    if db["status"] != OBSERVED:
        return {"verdict": UNAVAILABLE, "reason": "database not observed"}
    if config.get("folder") is not None:
        return {"verdict": UNAVAILABLE, "reason": "folder configured but not observable"}
    ok = name["value"] == exp_name and db["value"] == exp_db
    return {"verdict": MATCH if ok else MISMATCH, "compared": "name+database"}


def require_match(report: Dict[str, Any]) -> None:
    if report.get("verdict") == MISMATCH:
        raise IdentityMismatch("open Resolve project does not match the configured identity")


# ---- CLI ---------------------------------------------------------------------------------------

def launch_resolve(exe: str, runner=subprocess.Popen) -> bool:
    """Start Resolve itself (no project argument). Only used on an explicit flag."""
    runner([exe])
    return True


def main(argv=None, *, process_lister=default_process_lister, socket_factory=default_socket_factory,
         runner=subprocess.Popen, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="resolve_discovery", description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="machine-local identity JSON (or env %s)" % CONFIG_ENV)
    ap.add_argument("--mcp-snapshot", help="JSON of read-only MCP replies")
    ap.add_argument("--start-bridge", action="store_true",
                    help="launch Resolve if not running (needs --resolve-exe); never starts the bridge script")
    ap.add_argument("--resolve-exe", help="path to the Resolve executable for --start-bridge")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
        mcp = None
        if args.mcp_snapshot:
            mcp = snapshot_transport(json.loads(Path(args.mcp_snapshot).read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        print(json.dumps({"verdict": UNAVAILABLE, "error": str(exc)[:120]}), file=out)
        return EXIT_CODES[UNAVAILABLE]
    launched = None
    if args.start_bridge and args.resolve_exe and check_running(process_lister).get("value") is False:
        launched = launch_resolve(args.resolve_exe, runner)
    report = discover(process_lister, socket_factory, mcp, config)
    if args.start_bridge:
        report["launch_resolve"] = launched
        report["bridge_start"] = "not automated: " + MANUAL_FALLBACK
    print(json.dumps(report, indent=2), file=out)
    return EXIT_CODES[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
