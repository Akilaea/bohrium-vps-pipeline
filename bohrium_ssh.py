#!/usr/bin/env python3
"""Bohrium remote command runner over SSH (password auth).

Default node credentials are read from create_node_result.json /
create_node_list.json, or can be passed explicitly.

Uses OpenSSH when available with a temporary known_hosts/askpass style
flow via paramiko pure-python SSH (preferred).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("bohrium_ssh")

DEFAULT_PROXY_HINT = "http://127.0.0.1:7890"


def load_json(path: Path) -> Any | None:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("load %s failed: %s", path, exc)
    return None


def find_node_credentials(
    *,
    node_id: int | None = None,
    prefer_name: str | None = None,
) -> dict[str, Any]:
    """Return {host/domain/ip, username, password, node_id, machine_id, name}."""
    candidates: list[dict[str, Any]] = []

    # create_node_result.json may be a result object or a list of nodes
    for path in (
        ROOT / "create_node_result.json",
        ROOT / "create_node_list.json",
        ROOT / "last_result.json",
    ):
        data = load_json(path)
        if data is None:
            continue
        if isinstance(data, dict):
            if data.get("ok") and (data.get("ip") or data.get("node_id")):
                info = data.get("node_info") or {}
                candidates.append(
                    {
                        "node_id": data.get("node_id") or info.get("nodeId"),
                        "machine_id": info.get("machineId"),
                        "name": info.get("nodeName") or data.get("payload", {}).get("name"),
                        "ip": data.get("ip") or info.get("ip"),
                        "domain": info.get("domainName") or info.get("domain") or "",
                        "username": data.get("username") or info.get("nodeUser") or "root",
                        "password": data.get("password") or info.get("nodePwd") or "",
                        "status": data.get("status") or info.get("status"),
                        "source": str(path.name),
                    }
                )
            # list-only style
            items = ((data.get("nodes") or {}).get("data") or {}).get("items") or []
            for item in items:
                candidates.append(
                    {
                        "node_id": item.get("nodeId"),
                        "machine_id": item.get("machineId"),
                        "name": item.get("nodeName") or item.get("name"),
                        "ip": item.get("ip"),
                        "domain": item.get("domainName") or item.get("domain") or "",
                        "username": item.get("nodeUser") or item.get("username") or "root",
                        "password": item.get("nodePwd") or item.get("password") or "",
                        "status": item.get("status"),
                        "source": str(path.name),
                    }
                )
            for cred in data.get("credentials") or []:
                candidates.append(
                    {
                        "node_id": cred.get("node_id") or cred.get("nodeId"),
                        "machine_id": cred.get("machine_id") or cred.get("machineId"),
                        "name": cred.get("node_name") or cred.get("name"),
                        "ip": cred.get("ip"),
                        "domain": cred.get("domain") or cred.get("domainName") or "",
                        "username": cred.get("username") or "root",
                        "password": cred.get("password") or "",
                        "status": cred.get("status"),
                        "source": str(path.name) + ":credentials",
                    }
                )
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                candidates.append(
                    {
                        "node_id": item.get("nodeId") or item.get("node_id"),
                        "machine_id": item.get("machineId") or item.get("machine_id"),
                        "name": item.get("name") or item.get("nodeName"),
                        "ip": item.get("ip"),
                        "domain": item.get("domain") or item.get("domainName") or "",
                        "username": item.get("username") or item.get("nodeUser") or "root",
                        "password": item.get("password") or item.get("nodePwd") or "",
                        "status": item.get("status"),
                        "source": str(path.name),
                    }
                )

    # de-dupe by node_id, keep richer password/domain
    by_id: dict[int, dict[str, Any]] = {}
    for c in candidates:
        nid = int(c.get("node_id") or 0)
        if not nid:
            continue
        prev = by_id.get(nid)
        if not prev:
            by_id[nid] = c
            continue
        # merge non-empty fields
        for k, v in c.items():
            if v and not prev.get(k):
                prev[k] = v

    if not by_id and not candidates:
        raise RuntimeError("no node credentials found; create a node first")

    selected = None
    if node_id is not None:
        selected = by_id.get(int(node_id))
        if not selected:
            raise RuntimeError(f"node_id={node_id} not found in local result files")
    elif prefer_name:
        for c in by_id.values():
            if str(c.get("name") or "") == prefer_name:
                selected = c
                break
    if selected is None:
        # newest / highest node_id
        if by_id:
            selected = by_id[max(by_id)]
        else:
            selected = candidates[0]

    if not selected.get("password"):
        raise RuntimeError(f"password missing for node {selected}")
    if not (selected.get("ip") or selected.get("domain")):
        raise RuntimeError(f"ip/domain missing for node {selected}")
    return selected


def tcp_open(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ssh_port(hosts: list[str], port: int = 22, timeout: float = 180.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for h in hosts:
            if not h:
                continue
            if tcp_open(h, port, timeout=2.0):
                LOG.info("ssh port open: %s:%s", h, port)
                return h
        time.sleep(2)
    return None


def connect_ssh(
    host: str,
    username: str,
    password: str,
    *,
    port: int = 22,
    timeout: float = 20.0,
):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    LOG.info("ssh connect %s@%s:%s", username, host, port)
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run_cmd(
    client,
    command: str,
    *,
    timeout: float | None = None,
    stream: bool = True,
) -> tuple[int, str, str]:
    LOG.info("$ %s", command)
    stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=timeout)
    chan = stdout.channel
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    while not chan.exit_status_ready():
        if chan.recv_ready():
            data = chan.recv(4096).decode("utf-8", "replace")
            out_chunks.append(data)
            if stream:
                sys.stdout.write(data)
                sys.stdout.flush()
        if chan.recv_stderr_ready():
            data = chan.recv_stderr(4096).decode("utf-8", "replace")
            err_chunks.append(data)
            if stream:
                sys.stderr.write(data)
                sys.stderr.flush()
        if timeout is not None and chan.exit_status_ready() is False:
            # paramiko timeout is on exec_command open; keep reading until done
            pass
        time.sleep(0.05)
    # drain remaining
    while chan.recv_ready():
        data = chan.recv(4096).decode("utf-8", "replace")
        out_chunks.append(data)
        if stream:
            sys.stdout.write(data)
            sys.stdout.flush()
    while chan.recv_stderr_ready():
        data = chan.recv_stderr(4096).decode("utf-8", "replace")
        err_chunks.append(data)
        if stream:
            sys.stderr.write(data)
            sys.stderr.flush()
    code = chan.recv_exit_status()
    if stream:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return code, "".join(out_chunks), "".join(err_chunks)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bohrium SSH remote command runner")
    p.add_argument("--host", default=None, help="ip or domain")
    p.add_argument("--ip", default=None)
    p.add_argument("--domain", default=None)
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--node-id", type=int, default=None)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--cmd", action="append", default=[], help="command to run (repeatable)")
    p.add_argument("--wget-sh", default=None, help="download this URL then sh the basename")
    p.add_argument("--wait-port", type=float, default=120.0, help="seconds to wait for port 22")
    p.add_argument("--timeout", type=float, default=None, help="per-command timeout seconds")
    p.add_argument("--json-out", type=Path, default=ROOT / "ssh_run_result.json")
    p.add_argument("-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.v else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    node = None
    try:
        node = find_node_credentials(node_id=args.node_id)
        LOG.info(
            "loaded node source=%s id=%s name=%s ip=%s domain=%s",
            node.get("source"),
            node.get("node_id"),
            node.get("name"),
            node.get("ip"),
            node.get("domain"),
        )
    except Exception as exc:  # noqa: BLE001
        if not (args.host or args.ip or args.domain):
            LOG.error("%s", exc)
            return 2
        LOG.warning("credential auto-load failed: %s", exc)
        node = {}

    host = args.host or args.domain or args.ip or node.get("domain") or node.get("ip")
    ip = args.ip or node.get("ip")
    domain = args.domain or node.get("domain")
    username = args.username or node.get("username") or "root"
    password = args.password or node.get("password") or ""
    hosts_try = []
    for h in (host, domain, ip):
        if h and h not in hosts_try:
            hosts_try.append(h)

    result: dict[str, Any] = {
        "ok": False,
        "node": {
            "node_id": (node or {}).get("node_id"),
            "name": (node or {}).get("name"),
            "ip": ip,
            "domain": domain,
            "username": username,
            "password": password,
            "host_used": None,
        },
        "outputs": [],
        "error": None,
    }

    if not password:
        result["error"] = "password empty"
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    ready_host = wait_ssh_port(hosts_try, port=args.port, timeout=args.wait_port)
    if not ready_host:
        result["error"] = f"ssh port not open on {hosts_try}"
        LOG.error(result["error"])
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    result["node"]["host_used"] = ready_host

    try:
        client = connect_ssh(ready_host, username, password, port=args.port)
    except Exception as exc:  # noqa: BLE001
        # try remaining hosts
        client = None
        last = exc
        for h in hosts_try:
            if h == ready_host:
                continue
            try:
                client = connect_ssh(h, username, password, port=args.port)
                result["node"]["host_used"] = h
                break
            except Exception as exc2:  # noqa: BLE001
                last = exc2
        if client is None:
            result["error"] = f"ssh connect failed: {last}"
            LOG.error(result["error"])
            args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

    try:
        cmds = list(args.cmd or [])
        if args.wget_sh:
            url = args.wget_sh
            base = url.rstrip("/").split("/")[-1] or "2.sh"
            cmds.extend(
                [
                    f"wget --no-check-certificate -O {base} '{url}'",
                    f"ls -la {base} && head -n 5 {base}",
                    f"sh {base}",
                ]
            )
        if not cmds:
            cmds = ["echo SSH_OK; uname -a; id; hostname; pwd"]

        for cmd in cmds:
            code, out, err = run_cmd(client, cmd, timeout=args.timeout, stream=True)
            result["outputs"].append(
                {"cmd": cmd, "exit_code": code, "output": out, "stderr": err}
            )
            if code != 0:
                LOG.warning("command exit=%s: %s", code, cmd)
                # for wget, fail early
                if cmd.startswith("wget"):
                    result["error"] = f"wget failed exit={code}"
                    result["ok"] = False
                    return 1
        result["ok"] = True
        return 0
    except Exception as exc:  # noqa: BLE001
        LOG.exception("ssh run failed")
        result["error"] = str(exc)
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("wrote %s", args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
