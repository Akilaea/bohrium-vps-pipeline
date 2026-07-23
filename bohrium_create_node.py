#!/usr/bin/env python3
"""Bohrium pure-protocol node creator (0.4 CNY/h by default).

Target page: https://www.bohrium.com/en/nodes

Observed protocol (authenticated with brmToken / Bearer JWT):

  GET  /bohrapi/v1/account/info
  GET  /bohrapi/v1/project/list
  GET  /bohrapi/v1/node/resources
  GET  /bohrapi/v1/node/resources/price?skuId=&projectId=&disk=
  GET  /bohrapi/v1/image/public/{imageId}/version
  POST /bohrapi/v1/node/add
  GET  /bohrapi/v1/node/list
  GET  /bohrapi/v1/node/{nodeId}   # detail: ip / nodeUser / nodePwd

Default machine (user-specified):
  skuId=419, imageId=37611, diskSize=20, device=container, platform=ali, turnoffAfter=-1

Default proxy: http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parent
DEFAULT_PROXY = "http://127.0.0.1:7890"
HOST = "https://www.bohrium.com"
LOGIN_REFERER = f"{HOST}/en/nodes"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# Global soft throttle when WAF/rate-limit (HTTP 405/429) is observed.
_API_COOLDOWN_UNTIL = 0.0
_API_COOLDOWN_LOCK = threading.Lock()
_RELEASE_SWEEP_LOCK = threading.Lock()
_LAST_RELEASE_SWEEP = 0.0

# empirically verified 0.4 CNY/h SKU on prod
TARGET_PRICE = None
DEFAULT_SKU_ID = 419
DEFAULT_SKU_LABEL = "sku-419"
DEFAULT_DISK = 20
DEFAULT_IMAGE_ID = 37611
# Start Nodes only uses container (VM option removed as unsupported shell)
DEFAULT_DEVICE = "container"
DEVICE_CONTAINER = "container"
DEFAULT_PLATFORM = "ali"
DEFAULT_TURNOFF_AFTER = -1
DEFAULT_PROJECT_ID = None  # each account has its own default project
DEFAULT_DATASETS: list = []

LOG = logging.getLogger("bohrium_create_node")


@dataclass
class CreateNodeResult:
    ok: bool
    node_id: int | None = None
    price: str | None = None
    sku_id: int | None = None
    sku_label: str | None = None
    project_id: int | None = None
    ip: str | None = None
    username: str | None = None
    password: str | None = None
    status: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    create_resp: dict[str, Any] | None = None
    node_info: dict[str, Any] | None = None
    error: str | None = None


class BohriumNodeClient:
    def __init__(
        self,
        token: str,
        *,
        proxy: str | None = DEFAULT_PROXY,
        host: str = HOST,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.token = token.strip()
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": self.host,
                "Referer": LOGIN_REFERER,
                "Authorization": f"Bearer {self.token}",
                "Content-Language": "en-US",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            }
        )
        # frontend stores these cookies
        self.session.cookies.set("brmToken", self.token, domain=".bohrium.com")
        self.session.cookies.set("sso-brmToken", self.token, domain=".bohrium.com")

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _wait_global_cooldown(self) -> None:
        with _API_COOLDOWN_LOCK:
            until = _API_COOLDOWN_UNTIL
        delay = until - time.time()
        if delay > 0:
            time.sleep(min(delay, 30.0))

    def _mark_rate_limited(self, seconds: float = 8.0) -> None:
        global _API_COOLDOWN_UNTIL
        with _API_COOLDOWN_LOCK:
            _API_COOLDOWN_UNTIL = max(_API_COOLDOWN_UNTIL, time.time() + seconds)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> dict[str, Any]:
        """HTTP with backoff on 405/429/5xx (WAF / rate limit under high concurrency)."""
        last_err: str | None = None
        for attempt in range(1, retries + 1):
            self._wait_global_cooldown()
            try:
                resp = self.session.request(
                    method,
                    self._url(path),
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_err = str(exc)
                time.sleep(min(1.5 * attempt, 8.0) + random.uniform(0, 0.5))
                continue

            status = int(resp.status_code)
            # WAF / gateway sometimes returns HTML 405 Not Allowed under flood
            if status in {405, 429, 502, 503, 504}:
                self._mark_rate_limited(6.0 + attempt * 2.0)
                last_err = f"HTTP {status}"
                LOG.warning(
                    "api %s %s -> %s (attempt %s/%s), backoff",
                    method,
                    path,
                    status,
                    attempt,
                    retries,
                )
                time.sleep(min(2.0 * attempt, 12.0) + random.uniform(0.2, 1.0))
                continue

            if method.upper() == "GET" and status >= 400:
                # business JSON with non-2xx is rare; surface cleanly
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
                if attempt < retries and status >= 500:
                    time.sleep(1.5 * attempt)
                    continue
                resp.raise_for_status()

            try:
                return resp.json()
            except Exception:
                return {"code": status, "raw": (resp.text or "")[:1000]}

        return {"code": 405, "error": {"msg": last_err or "request failed after retries"}, "raw": last_err or ""}

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        params = kwargs.pop("params", None)
        return self._request("GET", path, params=params)

    def post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", path, json_body=payload)

    def account_info(self) -> dict[str, Any]:
        return self.get("/bohrapi/v1/account/info")

    def project_list(self) -> dict[str, Any]:
        return self.get("/bohrapi/v1/project/list")

    def resources(self, **params: Any) -> dict[str, Any]:
        return self.get("/bohrapi/v1/node/resources", params=params or None)

    def resource_price(self, sku_id: int, project_id: int, disk: int = DEFAULT_DISK, scene: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"skuId": sku_id, "projectId": project_id, "disk": disk}
        if scene:
            params["scene"] = scene
        return self.get("/bohrapi/v1/node/resources/price", params=params)

    def image_versions(self, image_id: int = DEFAULT_IMAGE_ID, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        return self.get(
            f"/bohrapi/v1/image/public/{image_id}/version",
            params={"page": page, "pageSize": page_size},
        )

    def node_list(self, **params: Any) -> dict[str, Any]:
        return self.get("/bohrapi/v1/node/list", params=params or {"queryType": "private", "orderBy": "startTimeDesc"})

    def node_detail(self, node_id: int) -> dict[str, Any]:
        return self.get(f"/bohrapi/v1/node/{int(node_id)}")

    def node_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/bohrapi/v1/node/add", payload)

    def node_release(self, node_id: int) -> dict[str, Any]:
        """Release/delete a node. Primary API (from web UI):

          POST /bohrapi/v1/node/del/{nodeId}  body: {"nodeId": <id>}

        Fallbacks keep older path shapes for compatibility.
        """
        nid = int(node_id)
        last: dict[str, Any] = {}
        # Official delete path first (user-captured from create/container UI)
        candidates: list[tuple[str, dict[str, Any]]] = [
            (f"/bohrapi/v1/node/del/{nid}", {"nodeId": nid}),
            (f"/bohrapi/v1/node/del/{nid}", {"id": nid, "nodeId": nid}),
            ("/bohrapi/v1/node/del", {"nodeId": nid}),
            ("/bohrapi/v1/node/release", {"nodeId": nid}),
            ("/bohrapi/v1/node/release", {"id": nid, "nodeId": nid}),
            ("/bohrapi/v1/node/release", {"nodeId": nid, "force": True}),
            ("/bohrapi/v1/node/batchRelease", {"nodeIds": [nid]}),
            ("/bohrapi/v1/node/delete", {"nodeId": nid}),
            ("/bohrapi/v1/node/stop", {"nodeId": nid}),
            (f"/bohrapi/v1/node/{nid}/release", {"nodeId": nid}),
            (f"/bohrapi/v1/node/{nid}/delete", {"nodeId": nid}),
        ]
        for path, body in candidates:
            try:
                last = self.post(path, body)
                code = int(last.get("code", -1))
                LOG.info("node release try %s %s -> code=%s", path, body, code)
                if code == 0:
                    return last
            except Exception as exc:  # noqa: BLE001
                LOG.debug("release %s failed: %s", path, exc)
                last = {"error": str(exc), "path": path}
        return last

    def balance(self) -> dict[str, Any]:
        return self.get("/bohrapi/v1/account/user/integral")


def release_unready_nodes(
    client: BohriumNodeClient,
    *,
    keep_ready: bool = True,
    min_interval: float = 15.0,
) -> int:
    """Release nodes that have no IP/password (stuck / quota fillers).

    Rate-limited globally: under 20-way concurrency, avoid list+release storms
    that trigger HTTP 405 WAF blocks.
    """
    global _LAST_RELEASE_SWEEP
    with _RELEASE_SWEEP_LOCK:
        now = time.time()
        if now - _LAST_RELEASE_SWEEP < min_interval:
            return 0
        _LAST_RELEASE_SWEEP = now

    released = 0
    try:
        data = client.node_list(queryType="private", orderBy="startTimeDesc")
        if int(data.get("code", 0) or 0) in {405, 429}:
            LOG.warning("skip release sweep: list rate-limited %s", data.get("code"))
            return 0
        items = ((data.get("data") or {}).get("items") or [])
    except Exception as exc:  # noqa: BLE001
        LOG.warning("list nodes for release failed: %s", exc)
        return 0
    for item in items:
        nid = int(item.get("nodeId") or item.get("id") or 0)
        if not nid:
            continue
        # Ready nodes (have IP+pwd): keep when keep_ready=True
        if keep_ready and credentials_ready(item):
            continue
        # status=1 often means "creating"; without IP/pwd they still count toward
        # the 2-node project quota and must be force-released.
        st = item.get("status")
        LOG.warning(
            "releasing unready node id=%s status=%s device=%s ip=%s",
            nid,
            st,
            item.get("device"),
            item.get("ip") or "-",
        )
        try:
            resp = client.node_release(nid)
            ok = int((resp or {}).get("code", -1)) == 0
            if ok:
                released += 1
            else:
                LOG.warning("release node %s not confirmed: %s", nid, resp)
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("release unready %s failed: %s", nid, exc)
    if released:
        # Creating nodes may need a moment before quota frees up
        time.sleep(3.0)
    return released


def load_token(path: Path | None = None, token: str | None = None) -> str:
    if token:
        return token.strip()
    candidates = []
    if path:
        candidates.append(path)
    candidates.extend(
        [
            ROOT / "last_result.json",
            ROOT / "token.txt",
            ROOT / "auth.json",
        ]
    )
    for p in candidates:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if p.suffix.lower() == ".json":
            data = json.loads(text)
            for key in ("token", "brmToken", "access_token"):
                if data.get(key):
                    return str(data[key])
            cookies = data.get("cookies") or {}
            if cookies.get("brmToken"):
                return str(cookies["brmToken"])
        else:
            return text
    raise FileNotFoundError(
        "token not found; pass --token or provide last_result.json from bohrium_register.py"
    )


def pick_default_project(client: BohriumNodeClient, project_id: int | None = None) -> dict[str, Any]:
    data = client.project_list()
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(f"project list failed: {data}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("no project found for this account")
    if project_id is not None:
        for item in items:
            if int(item.get("id") or item.get("projectId") or 0) == int(project_id):
                return item
        raise RuntimeError(f"projectId={project_id} not found for this account")
    # each account has its own project id; prefer system default if present
    for item in items:
        name = str(item.get("name") or "").lower()
        if "default" in name or "system" in name:
            return item
    return items[0]


def _balance_value(bal_resp: dict[str, Any]) -> float:
    data = bal_resp.get("data") or {}
    if not isinstance(data, dict):
        return 0.0
    for key in ("balance", "orgBalance", "available"):
        try:
            return float(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


def wait_account_ready(
    client: BohriumNodeClient,
    *,
    project_id: int | None = None,
    min_balance: float = 1.0,
    timeout: float = 90.0,
    interval: float = 3.0,
) -> dict[str, Any]:
    """Wait until default project exists and free-credit balance is ready."""
    deadline = time.time() + max(timeout, 0.0)
    last_err = "account not ready"
    while time.time() < deadline:
        try:
            bal = client.balance()
            value = _balance_value(bal)
            LOG.info("balance poll: %s (value=%s)", bal.get("data"), value)
            if value < min_balance:
                last_err = f"balance too low: {value}"
                time.sleep(interval)
                continue
            project = pick_default_project(client, project_id=project_id)
            return project
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            LOG.warning("account not ready yet: %s", last_err)
            time.sleep(interval)
    raise RuntimeError(f"account not ready within {int(timeout)}s: {last_err}")


# Verified by create+node_detail probe (2026-07-23). NO guesses.
# Format: skuId -> (spec_label, cpu, mem_gb, is_gpu)
VERIFIED_SKU_SPECS: dict[int, tuple[str, int, float, bool]] = {
    # pure CPU (mining preferred)
    424: ("c64_m64_cpu", 64, 64.0, False),
    419: ("c64_m128_cpu", 64, 128.0, False),
    422: ("c64_m256_cpu", 64, 256.0, False),
    391: ("c32_m128_cpu", 32, 128.0, False),
    371: ("c16_m32_cpu", 16, 32.0, False),
    434: ("c8_m8_cpu", 8, 8.0, False),
    427: ("c8_m16_cpu", 8, 16.0, False),
    409: ("c4_m8_cpu", 4, 8.0, False),
    388: ("c2_m4_cpu", 2, 4.0, False),
    # GPU (excluded when cpu_only=True)
    428: ("c8_m31_1 * NVIDIA T4", 8, 31.0, True),
    372: ("c16_m62_1 * NVIDIA T4", 16, 62.0, True),
    365: ("c12_m92_1 * NVIDIA V100", 12, 92.0, True),
    390: ("c32_m128_4 * NVIDIA V100", 32, 128.0, True),
    421: ("c64_m256_8 * NVIDIA V100", 64, 256.0, True),
    426: ("c82_m336_8 * NVIDIA V100", 82, 336.0, True),
    402: ("c48_m368_4 * NVIDIA V100", 48, 368.0, True),
    438: ("c96_m372_4 * NVIDIA T4", 96, 372.0, True),
}

# Pure-CPU ids to inject when resources.cpuList is truncated for new accounts.
EXTRA_CPU_SKU_IDS: list[int] = [424, 419, 422, 391, 371, 434, 427, 409, 388]

# Runtime-learned map next to app (skuId -> {spec,cpu,memory,gpu})
SKU_MAP_PATH = ROOT / "sku_map.json"


def load_learned_sku_map() -> dict[int, dict[str, Any]]:
    """Load runtime-learned sku map from disk (merged with VERIFIED)."""
    out: dict[int, dict[str, Any]] = {}
    for sid, (label, cpu, mem, is_gpu) in VERIFIED_SKU_SPECS.items():
        out[sid] = {"label": label, "cpu": cpu, "mem": mem, "gpu": is_gpu, "source": "verified"}
    try:
        if SKU_MAP_PATH.is_file():
            raw = json.loads(SKU_MAP_PATH.read_text(encoding="utf-8"))
            for k, v in (raw or {}).items():
                try:
                    sid = int(k)
                except Exception:
                    continue
                if not isinstance(v, dict):
                    continue
                out[sid] = {
                    "label": str(v.get("label") or v.get("spec") or f"sku-{sid}"),
                    "cpu": int(v.get("cpu") or 0),
                    "mem": float(v.get("mem") or v.get("memory") or 0),
                    "gpu": bool(v.get("gpu") or ("NVIDIA" in str(v.get("label") or "") or "GPU" in str(v.get("label") or "").upper())),
                    "source": "learned",
                }
    except Exception as exc:  # noqa: BLE001
        LOG.debug("load sku_map failed: %s", exc)
    return out


def remember_sku_spec(sku_id: int, *, label: str, cpu: int, mem: float, gpu: bool = False) -> None:
    """Persist observed node detail so ranking never relies on guesses."""
    sid = int(sku_id)
    try:
        data: dict[str, Any] = {}
        if SKU_MAP_PATH.is_file():
            data = json.loads(SKU_MAP_PATH.read_text(encoding="utf-8")) or {}
        data[str(sid)] = {
            "label": label,
            "spec": label,
            "cpu": int(cpu or 0),
            "mem": float(mem or 0),
            "memory": float(mem or 0),
            "gpu": bool(gpu),
        }
        SKU_MAP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("learned sku map %s => %s c%s m%s gpu=%s", sid, label, cpu, mem, gpu)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("remember sku failed: %s", exc)


def list_skus(client: BohriumNodeClient, *, disk: int = DEFAULT_DISK) -> list[dict[str, Any]]:
    """Fetch SKUs. Uses same endpoint as web UI: GET /bohrapi/v1/node/resources.

    NOTE: For brand-new free accounts, backend often returns only 5 small CPU
    SKUs (up to c32_m128). Higher pure-CPU SKUs still work via node/add if we
    inject EXTRA_CPU_SKU_IDS (verified by create+detail probe).
    """
    learned = load_learned_sku_map()
    # Match frontend Pan({nodeType, isNotebookStart, creatorId, diskSize})
    creator_id = None
    try:
        info = client.account_info()
        creator_id = (info.get("data") or {}).get("userId")
    except Exception:
        pass
    params: dict[str, Any] = {
        "nodeType": 1,
        "isNotebookStart": False,
        "diskSize": int(disk),
    }
    if creator_id is not None:
        params["creatorId"] = creator_id
    data = client.get("/bohrapi/v1/node/resources", params=params)
    if int(data.get("code", -1)) != 0:
        data = client.resources()
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(f"resources failed: {data}")
    body = data.get("data") or {}
    out: list[dict[str, Any]] = []
    for item in body.get("cpuList") or []:
        sid = int(item.get("value"))
        label = str(item.get("label") or "")
        if sid in learned:
            label = str(learned[sid].get("label") or label)
        out.append({"kind": "cpu", "label": label, "skuId": sid, **item})
    for item in body.get("gpuList") or []:
        sid = int(item.get("value"))
        label = str(item.get("label") or "")
        if sid in learned:
            label = str(learned[sid].get("label") or label)
        out.append({"kind": "gpu", "label": label, "skuId": sid, **item})
    LOG.info(
        "resources cpuList=%s gpuList=%s",
        [(x.get("skuId"), x.get("label")) for x in out if x.get("kind") == "cpu"],
        [(x.get("skuId"), x.get("label")) for x in out if x.get("kind") == "gpu"],
    )
    return out


def parse_sku_spec(sku: dict[str, Any]) -> tuple[int, float]:
    """Return (cpu_cores, mem_gb) from label / known fields. Best-effort."""
    cpu = 0
    mem = 0.0
    for key in ("cpu", "cpuNum", "cpuCore", "cores", "vcpus"):
        try:
            v = int(sku.get(key) or 0)
            if v > 0:
                cpu = v
                break
        except (TypeError, ValueError):
            pass
    for key in ("memory", "mem", "memGb", "memoryGb", "ram"):
        try:
            v = float(sku.get(key) or 0)
            if v > 0:
                mem = v
                break
        except (TypeError, ValueError):
            pass
    text = " ".join(
        str(sku.get(k) or "")
        for k in ("label", "name", "spec", "value", "skuId")
    )
    # c64_m128 / 64c128g / 64核128G / 64C 128GB
    m = re.search(r"[cC](\d+)\s*[_\-]?[mM](\d+)", text)
    if m:
        cpu = max(cpu, int(m.group(1)))
        mem = max(mem, float(m.group(2)))
    m = re.search(r"(\d+)\s*[核cC]\s*(\d+(?:\.\d+)?)\s*[Gg]", text)
    if m:
        cpu = max(cpu, int(m.group(1)))
        mem = max(mem, float(m.group(2)))
    m = re.search(r"(\d+)\s*[Cc](?:ore)?s?\b", text)
    if m and cpu <= 0:
        cpu = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Gg](?:i?B)?", text)
    if m and mem <= 0:
        mem = float(m.group(1))
    # Do NOT invent cores from skuId (e.g. sku-401 -> 401 cores is wrong)
    return cpu, mem


def list_skus_ranked(
    client: BohriumNodeClient,
    project_id: int,
    *,
    disk: int = DEFAULT_DISK,
    cpu_only: bool = True,
    preferred_sku_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch SKUs and sort for mining: high CPU first, low memory within tier.

    Order:
      1) CPU cores high → low  (64c tier, then 52c, then 32c, ...)
      2) Within same CPU: memory low → high  (e.g. c64_m64 before c64_m128)
      3) price low → high, then skuId

    Exhausts all machines in a CPU tier before dropping to the next lower tier.
    Injects EXTRA_CPU_SKU_IDS when cpuList is truncated for new accounts.
    """
    learned = load_learned_sku_map()
    skus = list_skus(client, disk=disk)
    gpu_ids = {int(s["skuId"]) for s in skus if str(s.get("kind") or "").lower() == "gpu"}
    # also treat verified GPU skus as GPU even if only injected via map
    for sid, meta in learned.items():
        if meta.get("gpu"):
            gpu_ids.add(int(sid))

    if cpu_only:
        skus = [s for s in skus if str(s.get("kind") or "").lower() != "gpu"]

    by_id: dict[int, dict[str, Any]] = {int(s["skuId"]): dict(s) for s in skus}

    # Inject verified pure-CPU high-end ids missing from truncated cpuList
    for sid in EXTRA_CPU_SKU_IDS:
        if sid in by_id or sid in gpu_ids:
            continue
        meta = learned.get(sid) or {}
        if meta.get("gpu"):
            continue
        label = str(meta.get("label") or f"sku-{sid}")
        cpu = int(meta.get("cpu") or 0)
        mem = float(meta.get("mem") or 0)
        by_id[sid] = {
            "kind": "cpu",
            "skuId": sid,
            "label": label,
            "cpu": cpu,
            "mem": mem,
            "injected": True,
        }

    if preferred_sku_id is not None and int(preferred_sku_id) not in by_id:
        sid = int(preferred_sku_id)
        meta = learned.get(sid) or {}
        by_id[sid] = {
            "kind": "gpu" if meta.get("gpu") else "cpu",
            "skuId": sid,
            "label": str(meta.get("label") or f"sku-{sid}"),
            "cpu": int(meta.get("cpu") or 0),
            "mem": float(meta.get("mem") or 0),
            "injected": True,
        }

    ranked: list[dict[str, Any]] = []
    for sid, sku in by_id.items():
        meta = learned.get(sid)
        if cpu_only and (sid in gpu_ids or (meta and meta.get("gpu"))):
            continue
        if meta:
            label = str(meta.get("label") or sku.get("label") or f"sku-{sid}")
            cpu = int(meta.get("cpu") or 0)
            mem = float(meta.get("mem") or 0)
        else:
            label = str(sku.get("label") or "")
            cpu, mem = parse_sku_spec({**sku, "label": label})
        # skip pure-unknown injects with no cpu info
        if sku.get("injected") and cpu <= 0:
            continue
        price_val = 0.0
        price_str = ""
        try:
            price_resp = client.resource_price(sid, project_id, disk=disk)
            if int(price_resp.get("code", -1)) != 0:
                if sku.get("injected"):
                    LOG.debug("skip inject sku=%s price fail: %s", sid, price_resp)
                    continue
            else:
                price_str = str((price_resp.get("data") or {}).get("price") or "")
                price_val = float(price_str or 0)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("price sku=%s failed: %s", sid, exc)
            if sku.get("injected"):
                continue
        row = {
            **sku,
            "skuId": sid,
            "cpu": cpu,
            "mem": mem,
            "price": price_str,
            "price_val": price_val,
            "label": label or f"sku-{sid}",
            "gpu": bool(meta.get("gpu")) if meta else ("NVIDIA" in label or "GPU" in label.upper()),
        }
        ranked.append(row)
        LOG.info(
            "sku candidate id=%s label=%s cpu=%s mem=%s price=%s gpu=%s injected=%s",
            sid,
            row["label"],
            cpu,
            mem,
            price_str or "-",
            row["gpu"],
            bool(sku.get("injected")),
        )

    # CPU high→low; within tier mem low→high (mining prefers cores)
    ranked.sort(
        key=lambda x: (
            -int(x.get("cpu") or 0),
            float(x.get("mem") or 0),
            float(x.get("price_val") or 0),
            int(x["skuId"]),
        )
    )
    LOG.info(
        "sku order (cpu high→low, mem low→high, pure-CPU): %s",
        ", ".join(
            f"{x['skuId']}({x.get('label')}|c{x.get('cpu')}m{int(x.get('mem') or 0)})"
            for x in ranked[:20]
        ),
    )
    return ranked


def find_sku_by_price(
    client: BohriumNodeClient,
    project_id: int,
    *,
    target_price: float = TARGET_PRICE,
    disk: int = DEFAULT_DISK,
    preferred_sku_id: int | None = DEFAULT_SKU_ID,
) -> tuple[dict[str, Any], str]:
    skus = list_skus(client)
    # prefer known 0.4 sku first
    ordered = sorted(
        skus,
        key=lambda x: (0 if preferred_sku_id and x["skuId"] == preferred_sku_id else 1, x["skuId"]),
    )
    matches: list[tuple[dict[str, Any], str]] = []
    for sku in ordered:
        price_resp = client.resource_price(sku["skuId"], project_id, disk=disk)
        if int(price_resp.get("code", -1)) != 0:
            LOG.debug("price fail sku=%s resp=%s", sku, price_resp)
            continue
        price = str((price_resp.get("data") or {}).get("price") or "")
        LOG.info("sku=%s label=%s price=%s", sku["skuId"], sku["label"], price)
        try:
            if abs(float(price) - float(target_price)) < 1e-9:
                matches.append((sku, price))
        except ValueError:
            continue
    if not matches:
        raise RuntimeError(f"no sku found with price={target_price}")
    return matches[0]


def pick_cpu_image_version(
    client: BohriumNodeClient,
    *,
    image_id: int = DEFAULT_IMAGE_ID,
    version_id: int | None = None,
    image_name: str | None = None,
) -> dict[str, Any]:
    data = client.image_versions(image_id=image_id, page=1, page_size=50)
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(f"image versions failed: {data}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError(f"no versions for imageId={image_id}")
    if version_id is not None:
        for item in items:
            if int(item.get("id") or 0) == int(version_id):
                return item
        raise RuntimeError(f"versionId={version_id} not found under imageId={image_id}")
    # prefer pure CPU + docker url
    cpu_items = [x for x in items if "CPU" in str(x.get("resourceType") or "").upper() and "GPU" not in str(x.get("resourceType") or "").upper()]
    pool = cpu_items or items
    if image_name:
        for item in pool:
            if image_name in (item.get("url") or "") or image_name == item.get("version"):
                return item
    # smallest-ish stable default: first cpu item
    return pool[0]


def build_add_payload(
    *,
    project_id: int,
    sku_id: int,
    name: str,
    disk_size: int,
    image_version: dict[str, Any] | None = None,
    image_id: int | None = None,
    image_name: str | None = None,
    device: str = DEFAULT_DEVICE,
    platform: str = DEFAULT_PLATFORM,
    turnoff_after: int = DEFAULT_TURNOFF_AFTER,
    datasets: list | None = None,
) -> dict[str, Any]:
    # Payload shape matches frontend create-node form.
    # imageId may be a public image family/version id from the UI.
    if image_version:
        resolved_image_id = int(image_version.get("id") or image_id or DEFAULT_IMAGE_ID)
        resolved_image_name = str(
            image_name
            or image_version.get("url")
            or image_version.get("version")
            or ""
        )
    else:
        resolved_image_id = int(image_id if image_id is not None else DEFAULT_IMAGE_ID)
        resolved_image_name = str(image_name or "")

    payload: dict[str, Any] = {
        "name": name,
        "imageId": resolved_image_id,
        "skuId": int(sku_id),
        "diskSize": int(disk_size),
        "projectId": int(project_id),
        "platform": platform,
        "device": device,
        "turnoffAfter": int(turnoff_after),
        "datasets": list(datasets or DEFAULT_DATASETS or []),
    }
    if resolved_image_name:
        payload["imageName"] = resolved_image_name
    return payload


# Node status enum from web frontend (shared-hooks):
#   Creating=0, Waiting=1, Running=2, Stopping=3, Stopped=4,
#   Deleted=5, Failed=6, Hibernating=7
NODE_STATUS_CREATING = 0
NODE_STATUS_WAITING = 1
NODE_STATUS_RUNNING = 2
NODE_STATUS_STOPPING = 3
NODE_STATUS_STOPPED = 4
NODE_STATUS_DELETED = 5
NODE_STATUS_FAILED = 6
NODE_STATUS_HIBERNATING = 7

NODE_STATUS_LABELS: dict[int, str] = {
    0: "Creating/Preparing",
    1: "Waiting",
    2: "Running",
    3: "Stopping",
    4: "Stopped",
    5: "Deleted",
    6: "Failed",
    7: "Hibernating",
}


def extract_credentials(node: dict[str, Any] | None) -> dict[str, Any]:
    node = node or {}
    return {
        "ip": (node.get("ip") or "").strip() or None,
        "username": (node.get("nodeUser") or node.get("username") or "").strip() or None,
        "password": (node.get("nodePwd") or node.get("password") or "").strip() or None,
        "status": node.get("status"),
        "starting_up_msg": (node.get("startingUpMsg") or "").strip() or None,
        "estimate_start_time": (node.get("estimateStartTime") or "").strip() or None,
        "start_time": (node.get("startTime") or "").strip() or None,
        "node_id": node.get("nodeId") or node.get("id"),
        "node_name": node.get("nodeName") or node.get("name"),
    }


def credentials_ready(node: dict[str, Any] | None) -> bool:
    """Ready when Running and IP/user/password are present."""
    creds = extract_credentials(node)
    try:
        st = int(creds.get("status")) if creds.get("status") is not None else None
    except (TypeError, ValueError):
        st = None
    # Prefer status==Running; still accept credentials if present (compat).
    if st is not None and st != NODE_STATUS_RUNNING:
        return False
    return bool(creds["ip"] and creds["username"] and creds["password"])


def node_is_terminal_fail(node: dict[str, Any] | None) -> bool:
    try:
        st = int((node or {}).get("status"))
    except (TypeError, ValueError):
        return False
    return st in {
        NODE_STATUS_STOPPING,
        NODE_STATUS_STOPPED,
        NODE_STATUS_DELETED,
        NODE_STATUS_FAILED,
    }


def wait_node(
    client: BohriumNodeClient,
    node_id: int,
    *,
    timeout: float = 180.0,
    interval: float = 3.0,
    require_credentials: bool = True,
) -> dict[str, Any] | None:
    """Poll until Preparing/Creating finishes and credentials are ready.

    Does NOT sleep a fixed wall-clock only: loops on node detail status.
    status 0/1 = still preparing/waiting; status 2 + ip/pwd = done.
    timeout is a safety ceiling for stuck prepares / empty inventory.
    """
    deadline = time.time() + max(timeout, 5.0)
    last: dict[str, Any] | None = None
    last_sig = ""
    t0 = time.time()
    while time.time() < deadline:
        data = client.node_list(queryType="private", orderBy="startTimeDesc")
        items = ((data.get("data") or {}).get("items") or [])
        for item in items:
            if int(item.get("nodeId") or 0) == int(node_id):
                last = item
                break
        try:
            detail = client.node_detail(node_id)
            if int(detail.get("code", -1)) == 0 and isinstance(detail.get("data"), dict):
                merged = dict(last or {})
                merged.update(detail["data"] or {})
                merged["nodeId"] = int(node_id)
                last = merged
        except Exception as exc:
            LOG.debug("node detail poll failed: %s", exc)

        if last is not None:
            creds = extract_credentials(last)
            try:
                st_i = int(creds.get("status")) if creds.get("status") is not None else -1
            except (TypeError, ValueError):
                st_i = -1
            st_label = NODE_STATUS_LABELS.get(st_i, f"status={creds.get('status')}")
            msg = creds.get("starting_up_msg") or ""
            sig = (
                f"{creds.get('status')}|{msg}|{creds.get('ip')}|"
                f"{bool(creds.get('password'))}|{creds.get('estimate_start_time')}"
            )
            if sig != last_sig:
                last_sig = sig
                elapsed = int(time.time() - t0)
                LOG.info(
                    "node prepare id=%s phase=%s elapsed=%ss ip=%s pwd=%s msg=%s est=%s",
                    node_id,
                    st_label,
                    elapsed,
                    creds.get("ip") or "-",
                    "yes" if creds.get("password") else "no",
                    msg or "-",
                    creds.get("estimate_start_time") or "-",
                )
            if node_is_terminal_fail(last):
                LOG.warning(
                    "node %s terminal phase=%s — stop waiting",
                    node_id,
                    st_label,
                )
                return last
            if not require_credentials:
                return last
            # Only accept when preparing done (Running) + credentials
            if credentials_ready(last):
                LOG.info(
                    "node prepare done id=%s phase=%s elapsed=%ss ip=%s",
                    node_id,
                    st_label,
                    int(time.time() - t0),
                    creds.get("ip"),
                )
                return last
        time.sleep(interval)
    if last is not None:
        creds = extract_credentials(last)
        try:
            st_i = int(creds.get("status")) if creds.get("status") is not None else -1
        except (TypeError, ValueError):
            st_i = -1
        LOG.warning(
            "node prepare timeout id=%s phase=%s elapsed=%ss ip=%s pwd=%s msg=%s",
            node_id,
            NODE_STATUS_LABELS.get(st_i, str(creds.get("status"))),
            int(time.time() - t0),
            creds.get("ip") or "-",
            "yes" if creds.get("password") else "no",
            creds.get("starting_up_msg") or "-",
        )
    return last


def _is_quota_limit(msg: str, code: int) -> bool:
    """Same-project node cap (e.g. max 2 nodes). Do NOT treat as balance retry."""
    if code == 140111:
        return True
    m = (msg or "").lower()
    raw = msg or ""
    return (
        "maximum number of your nodes" in m
        or ("max" in m and "node" in m and "project" in m)
        or ("最多" in raw and "节点" in raw)
        or "release your node" in m
    )


def _is_balance_error(msg: str, code: int) -> bool:
    """Free-credit / recharge issues — retry wait, do NOT switch SKU.

    Note: code 148888 is overloaded — also used for "no resource for the
    selected machine" (capacity). Prefer message text over bare code.
    """
    m = (msg or "").lower()
    raw = msg or ""
    # capacity disguised as 148888 — not balance
    if (
        "no resource" in m
        or "select again" in m
        or "请重新选择" in raw
        or "无可用资源" in raw
        or "没有资源" in raw
    ):
        return False
    if "balance" in m or "recharge" in m or "integral" in m:
        return True
    if "余额" in raw or "充值" in raw or "积分" in raw:
        return True
    # only treat 148888 as balance when message looks like money/credit
    if code == 148888 and (
        "balance" in m or "recharge" in m or "insufficient" in m or "余额" in raw or "充值" in raw
    ):
        return True
    return False


def _is_auth_error(msg: str, code: int) -> bool:
    if code in {401, 403, 140001, 140003}:
        return True
    m = (msg or "").lower()
    raw = msg or ""
    return (
        "unauthorized" in m
        or ("token" in m and "invalid" in m)
        or "登录" in raw
        or "未登录" in raw
    )


def _is_rate_limited(msg: str, code: int) -> bool:
    if code in {405, 429, 502, 503, 504}:
        return True
    m = (msg or "").lower()
    raw = (msg or "") + " " + m
    return (
        "not allowed" in m
        or "too many" in m
        or "rate limit" in m
        or "doctypehtml" in m.replace(" ", "")
        or "<!doctype" in m
    )


def _sku_fail_is_capacity(msg: str, code: int) -> bool:
    """True only for stock/schedule capacity — safe to try lower SKU."""
    if (
        _is_quota_limit(msg, code)
        or _is_balance_error(msg, code)
        or _is_auth_error(msg, code)
        or _is_rate_limited(msg, code)
    ):
        return False
    m = (msg or "").lower()
    keys = (
        "sold out",
        "no available",
        "out of stock",
        "no stock",
        "no resource",
        "select again",
        "capacity",
        "schedule",
        "库存",
        "无可用",
        "售罄",
        "资源不足",
        "没有资源",
        "请重新选择",
        "算力",
        "排队",
        "繁忙",
        "not enough resource",
        "resource not enough",
    )
    if any(k in m for k in keys):
        return True
    # avoid bare "resource"/"insufficient" — matches balance errors
    return code in {140404, 500}


def normalize_device(device: str | None) -> str:
    """Always container for Start Nodes (vm removed)."""
    return DEVICE_CONTAINER


def create_node(
    *,
    token: str,
    proxy: str | None = DEFAULT_PROXY,
    target_price: float = TARGET_PRICE,
    sku_id: int | None = None,
    project_id: int | None = None,
    disk_size: int = DEFAULT_DISK,
    name: str = "node-auto",
    image_id: int = DEFAULT_IMAGE_ID,
    version_id: int | None = None,
    image_name: str | None = None,
    device: str = DEFAULT_DEVICE,
    dry_run: bool = False,
    wait: bool = True,
    sku_fallback: bool = True,
    wait_timeout: float = 180.0,
) -> CreateNodeResult:
    device = normalize_device(device)
    client = BohriumNodeClient(token, proxy=proxy)
    try:
        info = client.account_info()
        LOG.info("account: %s", json.dumps(info.get("data") or {}, ensure_ascii=False)[:300])
        bal = client.balance()
        LOG.info("balance: %s", bal.get("data"))

        # New accounts often need a few seconds before project/credits appear.
        project = wait_account_ready(client, project_id=project_id, min_balance=1.0, timeout=90.0)
        pid = int(project.get("id") or project.get("projectId"))
        LOG.info("project id=%s name=%s", pid, project.get("name"))

        # Build SKU try-list: high-spec → low-spec (or single fixed SKU).
        sku_try: list[dict[str, Any]] = []
        if sku_fallback or sku_id is None:
            try:
                ranked = list_skus_ranked(
                    client,
                    pid,
                    disk=disk_size,
                    cpu_only=True,
                    preferred_sku_id=sku_id or DEFAULT_SKU_ID,
                )
                sku_try = ranked
            except Exception as exc:  # noqa: BLE001
                LOG.warning("list ranked skus failed: %s", exc)
        if not sku_try:
            if sku_id is not None:
                sid0 = int(sku_id)
                price_resp = client.resource_price(sid0, pid, disk=disk_size)
                price0 = str((price_resp.get("data") or {}).get("price") or "")
                sku_try = [
                    {
                        "skuId": sid0,
                        "label": DEFAULT_SKU_LABEL if sid0 == DEFAULT_SKU_ID else str(sid0),
                        "price": price0,
                    }
                ]
            elif target_price is not None:
                sku, price = find_sku_by_price(
                    client,
                    pid,
                    target_price=target_price,
                    disk=disk_size,
                    preferred_sku_id=DEFAULT_SKU_ID,
                )
                sku_try = [{**sku, "price": price}]
            else:
                sku_try = [{"skuId": DEFAULT_SKU_ID, "label": DEFAULT_SKU_LABEL, "price": ""}]

        # If user fixed sku_id without fallback, only that SKU; with fallback keep pure high→low.
        if sku_id is not None and not sku_fallback:
            sku_try = [x for x in sku_try if int(x["skuId"]) == int(sku_id)] or [
                {"skuId": int(sku_id), "label": str(sku_id), "price": ""}
            ]

        image_version = None
        resolved_image_id = int(image_id)
        resolved_image_name = image_name
        if version_id is not None or image_name:
            image_version = pick_cpu_image_version(
                client,
                image_id=image_id,
                version_id=version_id,
                image_name=image_name,
            )
            resolved_image_id = int(image_version.get("id") or image_id)
            resolved_image_name = str(
                image_name
                or image_version.get("url")
                or image_version.get("version")
                or ""
            )
            LOG.info(
                "image version id=%s url=%s resourceType=%s",
                image_version.get("id"),
                image_version.get("url"),
                image_version.get("resourceType"),
            )
        else:
            LOG.info("using direct imageId=%s (skip public image version resolve)", resolved_image_id)

        if dry_run:
            first = sku_try[0]
            payload = build_add_payload(
                project_id=pid,
                sku_id=int(first["skuId"]),
                name=name,
                disk_size=disk_size,
                image_version=image_version,
                image_id=resolved_image_id,
                image_name=resolved_image_name,
                device=device,
                platform=DEFAULT_PLATFORM,
                turnoff_after=DEFAULT_TURNOFF_AFTER,
                datasets=DEFAULT_DATASETS,
            )
            return CreateNodeResult(
                ok=True,
                price=str(first.get("price") or ""),
                sku_id=int(first["skuId"]),
                sku_label=str(first.get("label") or first["skuId"]),
                project_id=pid,
                payload={**payload, "sku_try": [int(x["skuId"]) for x in sku_try]},
                error=None,
            )

        errors: list[str] = []
        last_payload: dict[str, Any] = {}
        last_create: dict[str, Any] | None = None
        last_price = ""
        last_sid = int(sku_try[0]["skuId"])
        last_label = str(sku_try[0].get("label") or last_sid)
        # Drop leftover unready nodes so SKU fallback / new creates don't hit 2-node cap.
        try:
            n0 = release_unready_nodes(client, keep_ready=True, min_interval=15.0)
            if n0:
                LOG.info("pre-create released unready nodes: %s", n0)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("pre-create cleanup: %s", exc)

        # Desync concurrent tasks slightly (20 threads otherwise stampede APIs).
        time.sleep(random.uniform(0.0, 2.5))

        for idx, sku in enumerate(sku_try, start=1):
            sid = int(sku["skuId"])
            label = str(sku.get("label") or sid)
            price = str(sku.get("price") or "")
            if not price:
                try:
                    pr = client.resource_price(sid, pid, disk=disk_size)
                    price = str((pr.get("data") or {}).get("price") or "")
                except Exception:
                    price = ""
            last_sid, last_label, last_price = sid, label, price
            LOG.info(
                "try sku %s/%s id=%s label=%s price=%s cpu=%s mem=%s",
                idx,
                len(sku_try),
                sid,
                label,
                price or "-",
                sku.get("cpu"),
                sku.get("mem"),
            )
            payload = build_add_payload(
                project_id=pid,
                sku_id=sid,
                name=name if idx == 1 else f"{name}-s{sid}",
                disk_size=disk_size,
                image_version=image_version,
                image_id=resolved_image_id,
                image_name=resolved_image_name,
                device=device,
                platform=DEFAULT_PLATFORM,
                turnoff_after=DEFAULT_TURNOFF_AFTER,
                datasets=DEFAULT_DATASETS,
            )
            last_payload = payload

            create_resp: dict[str, Any] | None = None
            for attempt in range(1, 4):
                create_resp = client.node_add(payload)
                # single-line summary; full body only on non-zero
                code = int(create_resp.get("code", -1))
                last_create = create_resp
                if code == 0:
                    LOG.info("create resp sku=%s attempt=%s: ok", sid, attempt)
                    break
                msg = str(((create_resp.get("error") or {}).get("msg") or create_resp))
                LOG.info("create resp sku=%s attempt=%s: code=%s msg=%s", sid, attempt, code, msg[:200])
                if _is_auth_error(msg, code):
                    LOG.error("auth error, stop: %s", msg)
                    break
                # HTTP 405/429 WAF under concurrency — backoff, do not burn SKUs
                if _is_rate_limited(msg, code):
                    LOG.warning("rate-limited/WAF code=%s; backoff then %s", code, "retry" if attempt < 3 else "stop")
                    time.sleep(3.0 * attempt + random.uniform(0.5, 2.0))
                    if attempt < 3:
                        continue
                    break
                # Quota: release stuck nodes once, retry add at most once — never spam
                if _is_quota_limit(msg, code):
                    n = release_unready_nodes(client, keep_ready=True, min_interval=20.0)
                    LOG.warning(
                        "node quota hit code=%s (released unready=%s); %s",
                        code,
                        n,
                        "retry once" if attempt == 1 else "stop",
                    )
                    if attempt == 1 and n > 0:
                        time.sleep(3.0)
                        continue
                    break
                # Balance / free-credit race only
                if _is_balance_error(msg, code):
                    if attempt >= 3:
                        LOG.warning("balance still insufficient after retries; stop sku tries")
                        break
                    LOG.warning("balance transient code=%s; wait account ready (%s/3)", code, attempt)
                    time.sleep(2.0 * attempt)
                    try:
                        project = wait_account_ready(
                            client, project_id=project_id, min_balance=1.0, timeout=20.0, interval=2.0
                        )
                        pid = int(project.get("id") or project.get("projectId") or pid)
                        payload["projectId"] = pid
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("re-wait account ready failed: %s", exc)
                    continue
                # Capacity / other → break inner loop, maybe next SKU
                break

            assert create_resp is not None
            code = int(create_resp.get("code", -1))
            if code != 0:
                msg = str(
                    ((create_resp.get("error") or {}).get("msg") if isinstance(create_resp.get("error"), dict) else None)
                    or create_resp.get("raw")
                    or create_resp
                )
                errors.append(f"sku={sid} add_fail: {str(msg)[:180]}")
                if _is_auth_error(msg, code) or _is_balance_error(msg, code):
                    LOG.warning("non-sku-switchable error, stop fallback: %s", str(msg)[:160])
                    break
                if _is_rate_limited(msg, code):
                    LOG.warning("rate-limited during create; stop sku fallback (avoid 405 cascade)")
                    break
                if _is_quota_limit(msg, code):
                    LOG.warning("project node quota still hit after cleanup; stop sku fallback")
                    break
                if sku_fallback and _sku_fail_is_capacity(msg, code) and idx < len(sku_try):
                    LOG.warning("sku=%s capacity fail, try next lower spec", sid)
                    continue
                # unknown error: try next SKU only if not HTML/WAF junk
                if sku_fallback and idx < len(sku_try) and not _is_rate_limited(msg, code):
                    LOG.warning("sku=%s add failed (unknown), try next lower: %s", sid, str(msg)[:120])
                    continue
                break

            node_id = int(((create_resp.get("data") or {}).get("id")) or 0) or None
            node_info = None
            if wait and node_id:
                # Wait on Preparing/Creating progress (status 0/1 → 2 Running + pwd),
                # not a blind sleep. timeout is only a stuck-prepare ceiling.
                # Intermediate SKUs use a shorter ceiling so empty inventory falls
                # through; last SKU uses full wait_timeout.
                remain = len(sku_try) - idx
                to = float(wait_timeout)
                if sku_fallback and remain > 0:
                    to = min(to, 90.0)
                node_info = wait_node(
                    client, node_id, timeout=to, interval=3, require_credentials=True
                )
                LOG.info("node info: %s", json.dumps(node_info or {}, ensure_ascii=False)[:500])
            # Learn real spec from detail ASAP (even if IP never arrives)
            if node_info:
                real_spec = str(node_info.get("spec") or "")
                real_cpu = int(node_info.get("cpu") or 0)
                real_mem = float(node_info.get("memory") or 0)
                if real_spec or real_cpu:
                    is_gpu = "NVIDIA" in real_spec or "GPU" in real_spec.upper() or bool(node_info.get("gpu") and str(node_info.get("gpu")) not in {"", "-", "0", "None"})
                    remember_sku_spec(
                        sid,
                        label=real_spec or label,
                        cpu=real_cpu,
                        mem=real_mem,
                        gpu=is_gpu,
                    )
                    if real_spec:
                        label = real_spec
            creds = extract_credentials(node_info)
            if creds.get("ip") and creds.get("password"):
                status_val = creds.get("status")
                return CreateNodeResult(
                    ok=True,
                    node_id=node_id,
                    price=price,
                    sku_id=sid,
                    sku_label=label,
                    project_id=pid,
                    ip=creds.get("ip"),
                    username=creds.get("username"),
                    password=creds.get("password"),
                    status=int(status_val)
                    if status_val is not None and str(status_val).isdigit()
                    else status_val,
                    payload=payload,
                    create_resp=create_resp,
                    node_info=node_info,
                )

            # Node created but credentials never ready → release this node, optional next SKU
            errors.append(f"sku={sid} node={node_id} no credentials in time")
            if node_id:
                try:
                    client.node_release(node_id)
                    time.sleep(1.5)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("release node %s failed: %s", node_id, exc)
            # Sweep at most every 15s globally (prevents 20-thread list storms → HTTP 405)
            try:
                release_unready_nodes(client, keep_ready=True, min_interval=15.0)
            except Exception:
                pass
            if not sku_fallback or idx >= len(sku_try):
                break
            LOG.warning("sku=%s not ready, fallback to next lower spec", sid)
            continue

        return CreateNodeResult(
            ok=False,
            price=last_price,
            sku_id=last_sid,
            sku_label=last_label,
            project_id=pid,
            payload=last_payload,
            create_resp=last_create,
            error="all sku tries failed: " + " | ".join(errors[-8:]),
        )
    except Exception as exc:
        LOG.exception("create_node failed")
        return CreateNodeResult(ok=False, error=str(exc))


def result_to_dict(result: CreateNodeResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "node_id": result.node_id,
        "price": result.price,
        "sku_id": result.sku_id,
        "sku_label": result.sku_label,
        "project_id": result.project_id,
        "ip": result.ip,
        "username": result.username,
        "password": result.password,
        "status": result.status,
        "payload": result.payload,
        "create_resp": result.create_resp,
        "node_info": result.node_info,
        "error": result.error,
    }


def print_credentials(result: CreateNodeResult | dict[str, Any]) -> None:
    if isinstance(result, CreateNodeResult):
        data = result_to_dict(result)
    else:
        data = result
    print("")
    print("===== NODE CREDENTIALS =====")
    print(f"node_id : {data.get('node_id')}")
    print(f"ip      : {data.get('ip')}")
    print(f"username: {data.get('username')}")
    print(f"password: {data.get('password')}")
    print(f"status  : {data.get('status')}")
    print("============================")
    print("")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bohrium pure-protocol create node (default skuId=419 imageId=37611)")
    p.add_argument("--token", default=None, help="brmToken / JWT; default: read last_result.json")
    p.add_argument("--token-file", type=Path, default=None, help="json/text file containing token")
    p.add_argument("--proxy", default=DEFAULT_PROXY, help=f"HTTP proxy (default {DEFAULT_PROXY})")
    p.add_argument("--no-proxy", action="store_true")
    p.add_argument("--price", type=float, default=None, help="target hourly price in CNY")
    p.add_argument("--sku-id", type=int, default=DEFAULT_SKU_ID, help="force skuId (default: 419)")
    p.add_argument("--project-id", type=int, default=None, help="projectId; default auto-pick from current account")
    p.add_argument("--disk-size", type=int, default=DEFAULT_DISK)
    p.add_argument("--name", default=None, help="node name; random if omitted")
    p.add_argument("--image-id", type=int, default=DEFAULT_IMAGE_ID, help="image id from create form (default 37611)")
    p.add_argument("--version-id", type=int, default=None, help="image version id under image family")
    p.add_argument("--image-name", default=None, help="match version by url/version string")
    p.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help="Start Nodes 设备：固定 container",
    )
    p.add_argument("--dry-run", action="store_true", help="only resolve sku/image/payload, do not create")
    p.add_argument("--no-wait", action="store_true", help="do not poll node list after create")
    p.add_argument("--list-only", action="store_true", help="only list current nodes and prices")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    proxy = None if args.no_proxy else (args.proxy.strip() or None)
    token = load_token(args.token_file, args.token)
    client = BohriumNodeClient(token, proxy=proxy)

    if args.list_only:
        project = pick_default_project(client, project_id=args.project_id)
        pid = int(project.get("id") or project.get("projectId"))
        skus = list_skus(client)
        priced = []
        for sku in skus:
            pr = client.resource_price(sku["skuId"], pid, disk=args.disk_size)
            price = ((pr.get("data") or {}).get("price") if int(pr.get("code", -1)) == 0 else None)
            priced.append({**sku, "price": price})
        nodes = client.node_list(queryType="private", orderBy="startTimeDesc")
        items = ((nodes.get("data") or {}).get("items") or [])
        credentials = [extract_credentials(item) for item in items]
        payload = {
            "project": project,
            "skus": priced,
            "nodes": nodes,
            "credentials": credentials,
            "balance": client.balance(),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        print("")
        print("===== EXISTING NODE CREDENTIALS =====")
        for cred in credentials:
            print(
                f"node_id={cred.get('node_id')} name={cred.get('node_name')} "
                f"ip={cred.get('ip')} username={cred.get('username')} "
                f"password={cred.get('password')} status={cred.get('status')}"
            )
        print("=====================================")
        print("")
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        return 0

    node_name = args.name
    if not node_name:
        node_name = "node-%s" % time.strftime("%m%d%H%M%S")

    result = create_node(
        token=token,
        proxy=proxy,
        target_price=args.price,
        sku_id=args.sku_id,
        project_id=args.project_id,
        disk_size=args.disk_size,
        name=node_name,
        image_id=args.image_id,
        version_id=args.version_id,
        image_name=args.image_name,
        device=args.device,
        dry_run=args.dry_run,
        wait=not args.no_wait,
    )
    payload = result_to_dict(result)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if result.ok and not args.dry_run:
        print_credentials(result)
        if not (result.ip and result.username and result.password):
            LOG.warning("node created but credentials not ready yet; re-run --list-only later")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
