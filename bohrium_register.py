#!/usr/bin/env python3
"""Bohrium platform pure-protocol email register/login.

Flow (observed on https://platform.bohrium.com/login):
  1) create temporary mailbox
  2) optional Tencent slide captcha (aid=194611140, prod web)
  3) POST /api/account/code/send_by_email
  4) poll temp mailbox for 6-digit code
  5) POST /api/account/login/email_code  (auto-register unregistered email)

Default HTTP proxy: http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import secrets
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
SLIDE_DIR = PROJECT_ROOT / "slide"
if str(SLIDE_DIR) not in sys.path:
    sys.path.insert(0, str(SLIDE_DIR))

PLATFORM = "https://platform.bohrium.com"
API = f"{PLATFORM}/api"
LOGIN_PAGE = f"{PLATFORM}/login"

MAIL_API_URL = "https://mail.minecraft-cn.net"
MAIL_DOMAIN = "olsbvgq.shop"

# From frontend main.*.js (prod + non-miniprogram)
TENCENT_CAPTCHA_AID = 194611140
CAPTCHA_ENTRY_URL = LOGIN_PAGE
CAPTCHA_ENTRY_REFERER = PLATFORM + "/"

DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# sceneType int (validated by backend). 1 works for email login/register send.
DEFAULT_SCENE_TYPE = 1
BUSINESS_LINE = "bohrium"

LOG = logging.getLogger("bohrium_register")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class CaptchaTicket:
    ticket: str
    randstr: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegisterResult:
    ok: bool
    email: str
    mail_token: str
    code: str | None = None
    token: str | None = None
    captcha: CaptchaTicket | None = None
    send_resp: dict[str, Any] | None = None
    login_resp: dict[str, Any] | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Temp mail
# ---------------------------------------------------------------------------


class TempMail:
    def __init__(self, base_url: str = MAIL_API_URL, domain: str = MAIL_DOMAIN, verify_ssl: bool = False):
        self.base_url = base_url.rstrip("/")
        self.domain = domain
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl

    def create(self, prefix: str = "bohrium") -> tuple[str, str]:
        username = f"{prefix}_{secrets.token_hex(4)}"
        resp = self.session.post(
            f"{self.base_url}/api/v1/addresses",
            json={"username": username, "domain": self.domain},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        email = data["email"]
        token = data["token"]
        LOG.info("temp mailbox created: %s", email)
        return email, token

    def list_emails(self, token: str) -> list[dict[str, Any]]:
        resp = self.session.get(f"{self.base_url}/api/v1/{token}/emails", timeout=15)
        resp.raise_for_status()
        data = resp.json() or {}
        return list(data.get("emails") or [])

    def wait_code(self, token: str, timeout: int = 90, interval: float = 3.0) -> str | None:
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                emails = self.list_emails(token)
            except Exception as exc:
                LOG.warning("poll mailbox failed: %s", exc)
                time.sleep(interval)
                continue
            for item in emails:
                subject = str(item.get("subject") or "")
                body = str(item.get("body") or "")
                key = str(item.get("id") or subject)
                if key in seen:
                    continue
                seen.add(key)
                codes = re.findall(r"\d{6}", subject) or re.findall(r"\d{6}", body)
                if codes:
                    LOG.info("verification code found: %s (subject=%s)", codes[0], subject)
                    return codes[0]
            time.sleep(interval)
        return None


# ---------------------------------------------------------------------------
# Bohrium client
# ---------------------------------------------------------------------------


class BohriumClient:
    def __init__(
        self,
        proxy: str | None = DEFAULT_PROXY,
        user_agent: str = DEFAULT_UA,
        timeout: float = 30.0,
        language: str = "en-US",
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Content-Language": language,
                "Origin": PLATFORM,
                "Referer": LOGIN_PAGE,
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            }
        )

    def warm(self) -> None:
        resp = self.session.get(LOGIN_PAGE, timeout=self.timeout)
        resp.raise_for_status()
        LOG.debug("login page status=%s cookies=%s", resp.status_code, dict(self.session.cookies))

    def captcha_config(self) -> dict[str, Any]:
        resp = self.session.get(f"{API}/captcha/config", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def is_oversea(self) -> bool:
        resp = self.session.get(f"{API}/account/is_oversea", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json() or {}
        return bool(data.get("data"))

    def send_email_code(
        self,
        email: str,
        *,
        scene_type: int = DEFAULT_SCENE_TYPE,
        is_oversea: bool | None = None,
        refer: str = "",
        captcha: CaptchaTicket | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if is_oversea is None:
            try:
                is_oversea = self.is_oversea()
            except Exception:
                is_oversea = False
        payload: dict[str, Any] = {
            "email": email,
            "sceneType": int(scene_type),
            "businessLine": BUSINESS_LINE,
            "refer": refer,
            "isOversea": bool(is_oversea),
        }
        # Frontend currently does not attach captcha to email send, but keep fields
        # ready in case backend starts enforcing Tencent ticket validation.
        if captcha is not None:
            payload["ticket"] = captcha.ticket
            payload["randstr"] = captcha.randstr
        if extra:
            payload.update(extra)
        LOG.info("send email code -> %s sceneType=%s isOversea=%s", email, scene_type, is_oversea)
        resp = self.session.post(
            f"{API}/account/code/send_by_email",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        LOG.info("send email code response: %s", json.dumps(data, ensure_ascii=False)[:400])
        return data

    def login_email_code(
        self,
        email: str,
        code: str,
        *,
        channel: str = "pc",
        device: str = "pc",
        refer: dict[str, Any] | None = None,
        ext: str = "",
        captcha: CaptchaTicket | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": email,
            "code": code,
            "businessLine": BUSINESS_LINE,
            "refer": refer if refer is not None else {},
            "channel": channel,
            "device": device,  # backend expects string, not object
            "ext": ext,
        }
        if captcha is not None:
            payload["ticket"] = captcha.ticket
            payload["randstr"] = captcha.randstr
        if extra:
            payload.update(extra)
        LOG.info("login/register email_code -> %s", email)
        resp = self.session.post(
            f"{API}/account/login/email_code",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # token may be set as cookie brmToken / sso-brmToken
        LOG.info("login response code=%s keys=%s", data.get("code"), list((data.get("data") or {}).keys()))
        return data

    def auth_check(self, token: str | None = None) -> dict[str, Any]:
        headers = {}
        if token:
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        resp = self.session.get(f"{API}/account/auth/check", headers=headers, timeout=self.timeout)
        # endpoint may return empty body / various shapes
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "text": resp.text[:500]}


# ---------------------------------------------------------------------------
# Captcha (optional, uses project slide solver)
# ---------------------------------------------------------------------------


def solve_slide_captcha(
    proxy: str | None,
    *,
    aid: int = TENCENT_CAPTCHA_AID,
    retries: int = 3,
    seed: int | None = None,
) -> CaptchaTicket:
    try:
        from slide_solver import SlideSolver, SlideSolverConfig
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"cannot import slide_solver from {SLIDE_DIR}; ensure project slide deps are installed"
        ) from exc

    config = SlideSolverConfig(random_seed=seed)
    LOG.info("solving tencent slide captcha aid=%s proxy=%s retries=%s", aid, proxy, retries)
    with SlideSolver(
        proxy=proxy,
        config=config,
        aid=aid,
        entry_url=CAPTCHA_ENTRY_URL,
        entry_referer=CAPTCHA_ENTRY_REFERER,
    ) as solver:
        result = solver.solve(retries=retries)
    if str(result.get("errorCode", "")) != "0" or not result.get("ticket"):
        raise RuntimeError(f"slide captcha failed: {result}")
    ticket = str(result["ticket"])
    randstr = str(result.get("randstr") or result.get("randStr") or "")
    if not randstr:
        # some solver versions return only ticket; keep empty and let backend reject if needed
        randstr = ""
    LOG.info("captcha solved ticket_len=%s randstr=%s", len(ticket), randstr)
    return CaptchaTicket(ticket=ticket, randstr=randstr, raw=result)


# ---------------------------------------------------------------------------
# Register orchestration
# ---------------------------------------------------------------------------


def register_once(
    *,
    proxy: str | None = DEFAULT_PROXY,
    prefix: str = "bohrium",
    scene_type: int = DEFAULT_SCENE_TYPE,
    mail_timeout: int = 90,
    require_captcha: bool = False,
    captcha_retries: int = 3,
    captcha_seed: int | None = None,
    channel: str = "pc",
    device: str = "pc",
) -> RegisterResult:
    mail = TempMail()
    client = BohriumClient(proxy=proxy)
    email = ""
    mail_token = ""
    captcha: CaptchaTicket | None = None

    try:
        client.warm()
        try:
            cfg = client.captcha_config()
            LOG.info("captcha config: %s", cfg)
            use_captcha = bool((cfg.get("data") or {}).get("isUseNewCaptchaVerify"))
        except Exception as exc:
            LOG.warning("captcha config fetch failed: %s", exc)
            use_captcha = False

        email, mail_token = mail.create(prefix=prefix)

        if require_captcha or use_captcha:
            # Observed: email send currently succeeds without ticket even when
            # isUseNewCaptchaVerify=true. Still solve when forced or when you want
            # ticket ready for future enforcement / phone-path parity.
            if require_captcha:
                captcha = solve_slide_captcha(
                    proxy,
                    aid=TENCENT_CAPTCHA_AID,
                    retries=captcha_retries,
                    seed=captcha_seed,
                )
            else:
                LOG.info(
                    "backend reports isUseNewCaptchaVerify=true but email send path "
                    "does not currently require ticket; skip captcha unless --require-captcha"
                )

        send_resp = client.send_email_code(
            email,
            scene_type=scene_type,
            captcha=captcha,
        )
        if int(send_resp.get("code", -1)) != 0:
            return RegisterResult(
                ok=False,
                email=email,
                mail_token=mail_token,
                captcha=captcha,
                send_resp=send_resp,
                error=f"send code failed: {send_resp}",
            )

        code = mail.wait_code(mail_token, timeout=mail_timeout)
        if not code:
            return RegisterResult(
                ok=False,
                email=email,
                mail_token=mail_token,
                captcha=captcha,
                send_resp=send_resp,
                error="timeout waiting for email verification code",
            )

        login_resp = client.login_email_code(
            email,
            code,
            channel=channel,
            device=device,
            captcha=captcha,
        )
        if int(login_resp.get("code", -1)) != 0:
            return RegisterResult(
                ok=False,
                email=email,
                mail_token=mail_token,
                code=code,
                captcha=captcha,
                send_resp=send_resp,
                login_resp=login_resp,
                cookies=client.session.cookies.get_dict(),
                error=f"login/register failed: {login_resp}",
            )

        token = None
        data = login_resp.get("data") or {}
        if isinstance(data, dict):
            token = data.get("token") or data.get("brmToken")
        cookies = client.session.cookies.get_dict()
        token = token or cookies.get("brmToken") or cookies.get("sso-brmToken")

        return RegisterResult(
            ok=True,
            email=email,
            mail_token=mail_token,
            code=code,
            token=token,
            captcha=captcha,
            send_resp=send_resp,
            login_resp=login_resp,
            cookies=cookies,
        )
    except Exception as exc:
        LOG.exception("register_once failed")
        return RegisterResult(
            ok=False,
            email=email,
            mail_token=mail_token,
            captcha=captcha,
            cookies=client.session.cookies.get_dict(),
            error=str(exc),
        )


def result_to_dict(result: RegisterResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "email": result.email,
        "mail_token": result.mail_token,
        "code": result.code,
        "token": result.token,
        "cookies": result.cookies,
        "captcha": None
        if result.captcha is None
        else {
            "ticket": result.captcha.ticket,
            "randstr": result.captcha.randstr,
            "errorCode": result.captcha.raw.get("errorCode"),
        },
        "send_resp": result.send_resp,
        "login_resp": result.login_resp,
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bohrium pure-protocol email register/login")
    p.add_argument(
        "--proxy",
        default=DEFAULT_PROXY,
        help=f"HTTP proxy URL (default: {DEFAULT_PROXY}). Use empty string to disable.",
    )
    p.add_argument("--no-proxy", action="store_true", help="disable proxy")
    p.add_argument("--prefix", default="bohrium", help="temp email local-part prefix")
    p.add_argument("--scene-type", type=int, default=DEFAULT_SCENE_TYPE, help="send_by_email sceneType")
    p.add_argument("--mail-timeout", type=int, default=90, help="seconds to wait for email code")
    p.add_argument(
        "--require-captcha",
        action="store_true",
        help="force solve Tencent slide captcha (aid=194611140) before send",
    )
    p.add_argument("--captcha-retries", type=int, default=3)
    p.add_argument("--captcha-seed", type=int, default=None)
    p.add_argument("--channel", default="pc")
    p.add_argument("--device", default="pc", help="device string expected by backend")
    p.add_argument("--out", type=Path, default=None, help="write JSON result to file")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    proxy: str | None
    if args.no_proxy:
        proxy = None
    else:
        proxy = args.proxy.strip() or None

    result = register_once(
        proxy=proxy,
        prefix=args.prefix,
        scene_type=args.scene_type,
        mail_timeout=args.mail_timeout,
        require_captcha=args.require_captcha,
        captcha_retries=args.captcha_retries,
        captcha_seed=args.captcha_seed,
        channel=args.channel,
        device=args.device,
    )
    payload = result_to_dict(result)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        LOG.info("wrote %s", args.out)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
