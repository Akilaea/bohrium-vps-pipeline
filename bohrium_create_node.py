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
import sys
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

# empirically verified 0.4 CNY/h SKU on prod
TARGET_PRICE = None
DEFAULT_SKU_ID = 419
DEFAULT_SKU_LABEL = "sku-419"
DEFAULT_DISK = 20
DEFAULT_IMAGE_ID = 37611
DEFAULT_DEVICE = "container"
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

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self.session.get(self._url(path), timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        resp = self.session.post(self._url(path), json=payload, timeout=self.timeout, **kwargs)
        # backend often returns 200 even for business errors
        try:
            return resp.json()
        except Exception:
            return {"code": resp.status_code, "raw": resp.text[:1000]}

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

    def balance(self) -> dict[str, Any]:
        return self.get("/bohrapi/v1/account/user/integral")


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


def list_skus(client: BohriumNodeClient) -> list[dict[str, Any]]:
    data = client.resources()
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(f"resources failed: {data}")
    body = data.get("data") or {}
    out: list[dict[str, Any]] = []
    for item in body.get("cpuList") or []:
        out.append({"kind": "cpu", "label": item.get("label"), "skuId": int(item.get("value"))})
    for item in body.get("gpuList") or []:
        out.append({"kind": "gpu", "label": item.get("label"), "skuId": int(item.get("value"))})
    return out


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


def extract_credentials(node: dict[str, Any] | None) -> dict[str, Any]:
    node = node or {}
    return {
        "ip": (node.get("ip") or "").strip() or None,
        "username": (node.get("nodeUser") or node.get("username") or "").strip() or None,
        "password": (node.get("nodePwd") or node.get("password") or "").strip() or None,
        "status": node.get("status"),
        "node_id": node.get("nodeId") or node.get("id"),
        "node_name": node.get("nodeName") or node.get("name"),
    }


def credentials_ready(node: dict[str, Any] | None) -> bool:
    creds = extract_credentials(node)
    return bool(creds["ip"] and creds["username"] and creds["password"])


def wait_node(
    client: BohriumNodeClient,
    node_id: int,
    *,
    timeout: float = 180.0,
    interval: float = 3.0,
    require_credentials: bool = True,
) -> dict[str, Any] | None:
    """Poll list + detail until node appears and credentials are ready."""
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
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
            LOG.info(
                "node poll id=%s status=%s ip=%s user=%s pwd=%s",
                node_id,
                creds.get("status"),
                creds.get("ip") or "-",
                creds.get("username") or "-",
                ("*" * len(creds["password"])) if creds.get("password") else "-",
            )
            if not require_credentials or credentials_ready(last):
                return last
        time.sleep(interval)
    return last


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
) -> CreateNodeResult:
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

        if sku_id is None:
            sku, price = find_sku_by_price(
                client,
                pid,
                target_price=target_price,
                disk=disk_size,
                preferred_sku_id=DEFAULT_SKU_ID,
            )
            sid = int(sku["skuId"])
            label = str(sku.get("label") or "")
        else:
            sid = int(sku_id)
            price_resp = client.resource_price(sid, pid, disk=disk_size)
            if int(price_resp.get("code", -1)) != 0:
                raise RuntimeError(f"price query failed: {price_resp}")
            price = str((price_resp.get("data") or {}).get("price") or "")
            label = DEFAULT_SKU_LABEL if sid == DEFAULT_SKU_ID else str(sid)
            # still validate target price if user wants 0.4
            if target_price is not None and price:
                if abs(float(price) - float(target_price)) > 1e-9:
                    LOG.warning("sku=%s price=%s differs from target=%s", sid, price, target_price)

        image_version = None
        resolved_image_id = int(image_id)
        resolved_image_name = image_name
        # If caller already passes a concrete imageId from the UI form (e.g. 37611),
        # use it directly. Only resolve via public image family when version_id/image_name
        # are provided, or when image_id still looks like a family id.
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

        payload = build_add_payload(
            project_id=pid,
            sku_id=sid,
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
        LOG.info("create payload: %s", payload)
        if dry_run:
            return CreateNodeResult(
                ok=True,
                price=price,
                sku_id=sid,
                sku_label=label,
                project_id=pid,
                payload=payload,
                error=None,
            )

        create_resp: dict[str, Any] | None = None
        for attempt in range(1, 6):
            create_resp = client.node_add(payload)
            LOG.info("create resp attempt=%s: %s", attempt, create_resp)
            code = int(create_resp.get("code", -1))
            if code == 0:
                break
            msg = str(((create_resp.get("error") or {}).get("msg") or create_resp))
            # Transient: free credits / project init race right after register
            if code in {148888, 140404} or "balance" in msg.lower() or "project" in msg.lower():
                LOG.warning("node/add transient fail code=%s msg=%s; retry %s/5", code, msg, attempt)
                time.sleep(3.0 * attempt)
                try:
                    project = wait_account_ready(
                        client, project_id=project_id, min_balance=1.0, timeout=30.0, interval=2.0
                    )
                    pid = int(project.get("id") or project.get("projectId") or pid)
                    payload["projectId"] = pid
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("re-wait account ready failed: %s", exc)
                continue
            break
        assert create_resp is not None
        if int(create_resp.get("code", -1)) != 0:
            return CreateNodeResult(
                ok=False,
                price=price,
                sku_id=sid,
                sku_label=label,
                project_id=pid,
                payload=payload,
                create_resp=create_resp,
                error=f"node/add failed: {create_resp}",
            )
        node_id = int(((create_resp.get("data") or {}).get("id")) or 0) or None
        node_info = None
        if wait and node_id:
            node_info = wait_node(client, node_id, timeout=180, interval=3, require_credentials=True)
            LOG.info("node info: %s", json.dumps(node_info or {}, ensure_ascii=False)[:500])
        creds = extract_credentials(node_info)
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
            status=int(status_val) if status_val is not None and str(status_val).isdigit() else status_val,
            payload=payload,
            create_resp=create_resp,
            node_info=node_info,
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
    p.add_argument("--device", default=DEFAULT_DEVICE, help='backend device string, verified: "container"')
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
