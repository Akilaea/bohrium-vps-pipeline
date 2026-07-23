#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bohrium Tencent captcha multi-type router.

Tencent Waterwall can serve (risk-controlled, not fixed):
  - slide   滑块
  - image   图形点选
  - text    文字点选

Bohrium platform only enables TencentCaptcha (aid=194611140 prod web) when
/api/captcha/config.isUseNewCaptchaVerify=true. The concrete challenge type is
chosen by Tencent after prehandle, so we try available local solvers in order.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("captcha_multi")

ROOT = Path(__file__).resolve().parent
BYPASS = ROOT / "bypass"

# Bohrium prod web (non-miniprogram) app id from platform main.*.js
BOHRIUM_CAPTCHA_AID = 194611140
ENTRY_URL = "https://platform.bohrium.com/login"
ENTRY_REFERER = "https://platform.bohrium.com/"


@dataclass
class CaptchaTicket:
    ticket: str
    randstr: str
    kind: str = "unknown"
    raw: dict[str, Any] | None = None


def _ensure_path(p: Path) -> None:
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _ticket_from_result(result: dict[str, Any], kind: str) -> CaptchaTicket:
    if str(result.get("errorCode", "")) != "0" or not result.get("ticket"):
        raise RuntimeError(f"{kind} captcha failed: {result}")
    ticket = str(result["ticket"])
    randstr = str(result.get("randstr") or result.get("randStr") or "")
    return CaptchaTicket(ticket=ticket, randstr=randstr, kind=kind, raw=result)


def _solve_slide(proxy: str | None, *, aid: int, retries: int, seed: int | None) -> CaptchaTicket:
    _ensure_path(BYPASS / "slide")
    from slide_solver import SlideSolver, SlideSolverConfig  # type: ignore

    config = SlideSolverConfig(random_seed=seed)
    LOG.info("captcha try kind=slide aid=%s", aid)
    with SlideSolver(
        proxy=proxy,
        config=config,
        aid=aid,
        entry_url=ENTRY_URL,
        entry_referer=ENTRY_REFERER,
    ) as solver:
        result = solver.solve(retries=retries)
    return _ticket_from_result(result, "slide")


def _solve_image(proxy: str | None, *, aid: int, retries: int, seed: int | None) -> CaptchaTicket:
    # image_solver expects captcha_runtime (shared helper) on bypass root
    _ensure_path(BYPASS)
    _ensure_path(BYPASS / "image")
    try:
        import captcha_runtime  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "image solver needs bypass/captcha_runtime.py (missing); skip kind=image"
        ) from exc
    from image_solver import ImageSolver  # type: ignore

    LOG.info("captcha try kind=image aid=%s", aid)
    kwargs: dict[str, Any] = {
        "proxy": proxy,
        "aid": aid,
        "entry_url": ENTRY_URL,
    }
    try:
        solver = ImageSolver(**kwargs)
    except TypeError:
        solver = ImageSolver(proxy=proxy)
    try:
        if hasattr(solver, "solve"):
            result = solver.solve(retries=retries)
        else:
            raise RuntimeError("ImageSolver has no solve()")
    finally:
        if hasattr(solver, "close"):
            try:
                solver.close()
            except Exception:
                pass
    return _ticket_from_result(result, "image")


def _solve_text(proxy: str | None, *, aid: int, retries: int, seed: int | None) -> CaptchaTicket:
    _ensure_path(BYPASS)
    _ensure_path(BYPASS / "text")
    try:
        import captcha_runtime  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "text solver needs bypass/captcha_runtime.py (missing); skip kind=text"
        ) from exc
    from text_solver import TextSolver  # type: ignore

    LOG.info("captcha try kind=text aid=%s", aid)
    kwargs: dict[str, Any] = {
        "proxy": proxy,
        "aid": aid,
        "entry_url": ENTRY_URL,
    }
    try:
        solver = TextSolver(**kwargs)
    except TypeError:
        solver = TextSolver(proxy=proxy)
    try:
        result = solver.solve(retries=retries)
    finally:
        if hasattr(solver, "close"):
            try:
                solver.close()
            except Exception:
                pass
    return _ticket_from_result(result, "text")


# Bohrium live browser (Playwright) showed Tencent prehandle:
#   subcapclass=2408  → image click (点图), not pure slide.
# Order: image first for Bohrium, then slide, then text.
SOLVERS: list[tuple[str, Callable[..., CaptchaTicket]]] = [
    ("image", _solve_image),
    ("slide", _solve_slide),
    ("text", _solve_text),
]


def solve_tencent_captcha(
    proxy: str | None = None,
    *,
    aid: int = BOHRIUM_CAPTCHA_AID,
    retries: int = 2,
    seed: int | None = None,
    kinds: list[str] | None = None,
) -> CaptchaTicket:
    """Try available local solvers until one returns a ticket.

    kinds: optional subset of ["slide","image","text"]. Default: all.
    """
    want = set(kinds or ["slide", "image", "text"])
    errors: list[str] = []
    for name, fn in SOLVERS:
        if name not in want:
            continue
        try:
            ticket = fn(proxy, aid=aid, retries=retries, seed=seed)
            LOG.info("captcha solved kind=%s ticket_len=%s", ticket.kind, len(ticket.ticket))
            return ticket
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: {exc}"
            errors.append(msg)
            LOG.warning("captcha kind=%s failed: %s", name, exc)
            continue
    raise RuntimeError("all captcha solvers failed: " + " | ".join(errors[-6:]))
