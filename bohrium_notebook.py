#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bohrium Run Notebooks helper (second product mode).

Web flow (from frontend shared-hooks):
  GET  /bohrapi/v2/jupyter_server/check_start
  POST /bohrapi/v2/jupyter_server/start   {datasets:[], startType:0, projectId, creatorId, ...}
  GET  /bohrapi/v2/jupyter_server/status
  GET  /bohrapi/v2/jupyter_server/info
  POST /bohrapi/v2/jupyter_server/client_node  (optional node bind)
  GET  /bohrapi/v1/node/list  device=2 filterUsedFor=2  (notebook-linked nodes)

Mining path: if a linked node exposes ip/nodeUser/nodePwd, reuse SSH miner.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from bohrium_create_node import (
    BohriumNodeClient,
    DEFAULT_DISK,
    extract_credentials,
    list_skus_ranked,
    remember_sku_spec,
    wait_account_ready,
)

LOG = logging.getLogger("bohrium_notebook")


@dataclass
class NotebookResult:
    ok: bool
    project_id: int | None = None
    sku_id: int | None = None
    sku_label: str | None = None
    price: str | None = None
    node_id: int | None = None
    ip: str | None = None
    username: str | None = None
    password: str | None = None
    status: Any = None
    start_resp: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    node_info: dict[str, Any] | None = None
    error: str | None = None


def _as_int(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def notebook_check_start(client: BohriumNodeClient, *, project_id: int, creator_id: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {"projectId": int(project_id)}
    if creator_id is not None:
        params["creatorId"] = int(creator_id)
    return client.get("/bohrapi/v2/jupyter_server/check_start", params=params)


def notebook_start(
    client: BohriumNodeClient,
    *,
    project_id: int,
    creator_id: int | None,
    sku_id: int | None = None,
    disk_size: int = DEFAULT_DISK,
    image_id: int | None = None,
    start_type: int = 0,
    nb_token: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "projectId": int(project_id),
        "datasets": [],
        "startType": int(start_type),
    }
    if creator_id is not None:
        body["creatorId"] = int(creator_id)
    if sku_id is not None:
        body["skuId"] = int(sku_id)
    if disk_size:
        body["diskSize"] = int(disk_size)
    if image_id is not None:
        body["imageId"] = int(image_id)
    if nb_token:
        body["nbToken"] = nb_token
    if extra:
        body.update(extra)
    LOG.info("notebook start body=%s", body)
    return client.post("/bohrapi/v2/jupyter_server/start", body)


def notebook_status(client: BohriumNodeClient, *, project_id: int, creator_id: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"projectId": int(project_id)}
    if creator_id is not None:
        params["creatorId"] = int(creator_id)
    return client.get("/bohrapi/v2/jupyter_server/status", params=params)


def notebook_info(client: BohriumNodeClient, *, project_id: int, creator_id: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"projectId": int(project_id)}
    if creator_id is not None:
        params["creatorId"] = int(creator_id)
    return client.get("/bohrapi/v2/jupyter_server/info", params=params)


def list_notebook_nodes(client: BohriumNodeClient, *, project_id: int | None = None) -> list[dict[str, Any]]:
    """Notebook-linked nodes: frontend uses device=2, filterUsedFor=2."""
    params: dict[str, Any] = {
        "queryType": "private",
        "orderBy": "startTimeDesc",
        "device": 2,
        "filterUsedFor": 2,
        "status": 2,
    }
    if project_id is not None:
        params["projectId"] = int(project_id)
    data = client.get("/bohrapi/v1/node/list", params=params)
    items = ((data.get("data") or {}).get("items") or [])
    if items:
        return items
    # fallback: all private nodes
    data = client.node_list(queryType="private", orderBy="startTimeDesc")
    return ((data.get("data") or {}).get("items") or [])


def wait_notebook_ready(
    client: BohriumNodeClient,
    *,
    project_id: int,
    creator_id: int | None,
    timeout: float = 180.0,
    interval: float = 3.0,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Poll notebook status/info and linked nodes until SSH credentials or timeout."""
    deadline = time.time() + max(timeout, 5.0)
    last_info: dict[str, Any] | None = None
    last_node: dict[str, Any] | None = None
    last_sig = ""
    t0 = time.time()
    while time.time() < deadline:
        try:
            st = notebook_status(client, project_id=project_id, creator_id=creator_id)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("notebook status: %s", exc)
            st = {}
        try:
            inf = notebook_info(client, project_id=project_id, creator_id=creator_id)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("notebook info: %s", exc)
            inf = {}
        info_data = inf.get("data") if isinstance(inf.get("data"), dict) else {}
        st_data = st.get("data") if isinstance(st.get("data"), dict) else {}
        merged = {**st_data, **(info_data or {})}
        last_info = merged or last_info

        nodes = list_notebook_nodes(client, project_id=project_id)
        if not nodes:
            nodes = list_notebook_nodes(client, project_id=None)
        if nodes:
            last_node = nodes[0]
            # learn sku if present
            try:
                sid = _as_int(last_node.get("skuId") or last_node.get("machineId"))
                spec = str(last_node.get("spec") or "")
                cpu = int(last_node.get("cpu") or 0)
                mem = float(last_node.get("memory") or 0)
                if sid and (spec or cpu):
                    remember_sku_spec(
                        sid,
                        label=spec or f"sku-{sid}",
                        cpu=cpu,
                        mem=mem,
                        gpu="NVIDIA" in spec or "GPU" in spec.upper(),
                    )
            except Exception:
                pass

        creds = extract_credentials(last_node) if last_node else {
            "ip": (merged.get("ip") or "").strip() or None,
            "username": (merged.get("nodeUser") or merged.get("username") or "").strip() or None,
            "password": (merged.get("nodePwd") or merged.get("password") or "").strip() or None,
            "status": merged.get("status") or (last_node or {}).get("status"),
        }
        sig = f"{creds.get('status')}|{creds.get('ip')}|{bool(creds.get('password'))}|{merged.get('status')}"
        if sig != last_sig:
            last_sig = sig
            LOG.info(
                "notebook prepare elapsed=%ss status=%s node_status=%s ip=%s pwd=%s",
                int(time.time() - t0),
                merged.get("status") if merged else "-",
                (last_node or {}).get("status") if last_node else "-",
                creds.get("ip") or "-",
                "yes" if creds.get("password") else "no",
            )
        if creds.get("ip") and creds.get("password"):
            return last_info, last_node
        time.sleep(interval)
    return last_info, last_node


def create_notebook_runtime(
    *,
    token: str,
    proxy: str | None = None,
    sku_id: int | None = None,
    disk_size: int = DEFAULT_DISK,
    image_id: int | None = 37611,
    wait: bool = True,
    wait_timeout: float = 180.0,
    sku_fallback: bool = True,
) -> NotebookResult:
    """Start a notebook runtime; prefer pure-CPU SKUs high→low, low mem within tier."""
    client = BohriumNodeClient(token, proxy=proxy)
    try:
        info = client.account_info()
        uid = (info.get("data") or {}).get("userId")
        project = wait_account_ready(client, min_balance=0.0, timeout=60.0)
        pid = int(project.get("id") or project.get("projectId"))
        LOG.info("notebook project=%s creator=%s", pid, uid)

        try:
            chk = notebook_check_start(client, project_id=pid, creator_id=uid)
            LOG.info("check_start: %s", str(chk)[:300])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("check_start failed: %s", exc)

        sku_try: list[dict[str, Any]] = []
        if sku_id is not None:
            sku_try = [{"skuId": int(sku_id), "label": f"sku-{sku_id}", "price": ""}]
        else:
            try:
                sku_try = list_skus_ranked(client, pid, disk=disk_size, cpu_only=True)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("rank skus failed: %s", exc)
                sku_try = [{"skuId": 388, "label": "c2_m4_cpu", "price": "0.4"}]

        errors: list[str] = []
        last_start: dict[str, Any] | None = None
        last_label = ""
        last_price = ""
        last_sid: int | None = None

        for idx, sku in enumerate(sku_try, start=1):
            sid = int(sku["skuId"])
            label = str(sku.get("label") or sid)
            price = str(sku.get("price") or "")
            last_sid, last_label, last_price = sid, label, price
            LOG.info("notebook try sku %s/%s id=%s label=%s", idx, len(sku_try), sid, label)
            start_resp = notebook_start(
                client,
                project_id=pid,
                creator_id=uid,
                sku_id=sid,
                disk_size=disk_size,
                image_id=image_id,
            )
            last_start = start_resp
            code = int(start_resp.get("code", -1))
            msg = str(((start_resp.get("error") or {}) if isinstance(start_resp.get("error"), dict) else {}).get("msg") or start_resp)
            if code != 0:
                errors.append(f"sku={sid} start_fail: {msg[:160]}")
                LOG.warning("notebook start fail sku=%s: %s", sid, msg[:160])
                if not sku_fallback or idx >= len(sku_try):
                    break
                continue

            if not wait:
                return NotebookResult(
                    ok=True,
                    project_id=pid,
                    sku_id=sid,
                    sku_label=label,
                    price=price,
                    start_resp=start_resp,
                )

            info_data, node = wait_notebook_ready(
                client,
                project_id=pid,
                creator_id=uid,
                timeout=wait_timeout if idx >= len(sku_try) else min(wait_timeout, 90.0),
            )
            creds = extract_credentials(node) if node else {
                "ip": None,
                "username": None,
                "password": None,
                "status": None,
            }
            if node and (node.get("spec") or node.get("cpu")):
                label = str(node.get("spec") or label)
            if creds.get("ip") and creds.get("password"):
                return NotebookResult(
                    ok=True,
                    project_id=pid,
                    sku_id=sid,
                    sku_label=label,
                    price=price,
                    node_id=_as_int((node or {}).get("nodeId")),
                    ip=creds.get("ip"),
                    username=creds.get("username"),
                    password=creds.get("password"),
                    status=creds.get("status"),
                    start_resp=start_resp,
                    info=info_data,
                    node_info=node,
                )
            errors.append(f"sku={sid} no credentials after start")
            LOG.warning("notebook sku=%s started but no SSH credentials yet", sid)
            if not sku_fallback or idx >= len(sku_try):
                break

        return NotebookResult(
            ok=False,
            project_id=pid,
            sku_id=last_sid,
            sku_label=last_label,
            price=last_price,
            start_resp=last_start,
            error="notebook tries failed: " + " | ".join(errors[-8:]),
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("create_notebook_runtime failed")
        return NotebookResult(ok=False, error=str(exc))


def result_to_dict(result: NotebookResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "project_id": result.project_id,
        "sku_id": result.sku_id,
        "sku_label": result.sku_label,
        "price": result.price,
        "node_id": result.node_id,
        "ip": result.ip,
        "username": result.username,
        "password": result.password,
        "status": result.status,
        "start_resp": result.start_resp,
        "info": result.info,
        "node_info": result.node_info,
        "error": result.error,
        "product": "notebook",
    }
