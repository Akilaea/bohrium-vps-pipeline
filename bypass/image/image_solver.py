import time
"""
腾讯防水墙图形点选验证码 — 完整协议 (单文件)

aid=199999761 / subcapclass=2408
OCR: 纯 CV 多尺度旋转模板匹配 (内嵌)
"""
import argparse
import logging, json, math, os, re, sys, time
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urljoin

import cv2, numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from captcha_runtime import (  # noqa: E402
    CAPTCHA_BASE_URL,
    CaptchaRuntime,
    CaptchaRuntimeError,
    PointerEvent,
    RuntimeConfig,
)

PREHANDLE_URL = "https://turing.captcha.qcloud.com/cap_union_prehandle"
TDC_URL = "https://turing.captcha.qcloud.com/tdc.js"
VERIFY_URL = "https://turing.captcha.qcloud.com/cap_union_new_verify"
IMAGE_AID = 199999761
TDC_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tdc_server.js")

LOG = logging.getLogger(__name__)



def jsonp_parse(text):
    m = re.search(r"_aq_\d+\((.*)\)", text)
    return json.loads(m.group(1)) if m else json.loads(text)


# ====== Dataclasses ======
@dataclass
class Match:
    x: float; y: float; score: float

@dataclass(frozen=True)
class TplMask:
    mask: np.ndarray


# ====== CV sprite 分割 ======
def _black_mask(rgb, sprite=False):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if sprite:
        th = int(max(70, min(170, float(np.median(gray)) - 28)))
        return (gray < th).astype(np.uint8) * 255
    mx = rgb.max(axis=2); sp = mx - rgb.min(axis=2)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    local = (bh > 32) & (gray < 190)
    neutral = (gray < 125) & (mx < 155) & (sp < 58)
    return (np.uint8(local | neutral)) * 255


def _split_sprite(sprite_rgb):
    mask = cv2.morphologyEx(_black_mask(sprite_rgb, True), cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    n, lbs, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    comps = [(int(stats[i][0]), int(stats[i][1]), int(stats[i][0]+stats[i][2]),
              int(stats[i][1]+stats[i][3]), int(stats[i][4]), stats[i][0]+stats[i][2]/2.0)
             for i in range(1, n) if int(stats[i][4]) >= 8 and int(stats[i][2]) >= 2 and int(stats[i][3]) >= 2]

    if len(comps) >= 3:
        xs = np.array([c[5] for c in comps], dtype=np.float64)
        ws = np.array([c[4] for c in comps], dtype=np.float64)
        cents = np.array([xs.min(), np.median(xs), xs.max()], dtype=np.float64)
        lbs2 = np.zeros(len(comps), dtype=np.int32)
        for _ in range(25):
            d = np.abs(xs[:, None] - cents[None, :])
            nl = d.argmin(axis=1).astype(np.int32)
            for k in range(3):
                p = nl == k
                if p.any(): cents[k] = float(np.average(xs[p], weights=ws[p]))
            if np.array_equal(lbs2, nl) and np.allclose(cents, cents): break
            lbs2 = nl
        if len(set(map(int, lbs2))) == 3:
            templates = []
            h, w = mask.shape
            for k in range(3):
                g = [c for c, l in zip(comps, lbs2) if int(l) == k]
                if not g: break
                x1 = max(0, min(c[0] for c in g) - 3); y1 = max(0, min(c[1] for c in g) - 3)
                x2 = min(w, max(c[2] for c in g) + 4); y2 = min(h, max(c[3] for c in g) + 4)
                cm = mask[y1:y2, x1:x2]
                if int((cm > 0).sum()) >= 15: templates.append(TplMask(cm))
            if len(templates) == 3: return templates

    col = (mask > 0).sum(axis=0); act = np.where(col >= 1)[0]
    if len(act) == 0: raise ValueError("sprite empty")
    grps, st, pr = [], int(act[0]), int(act[0])
    for c in map(int, act[1:]):
        if c - pr > 8: grps.append([st, pr]); st = c
        pr = c
    grps.append([st, pr])
    while len(grps) > 3:
        gps = [grps[i+1][0] - grps[i][1] for i in range(len(grps)-1)]
        i = int(np.argmin(gps)); grps[i][1] = grps[i+1][1]; del grps[i+1]
    if len(grps) != 3:
        l, r = int(act[0]), int(act[-1])
        cuts = np.linspace(l, r+1, 4).astype(int)
        grps = [[int(cuts[i]), int(cuts[i+1]-1)] for i in range(3)]
    ret = []; h, w = mask.shape
    for x1, x2 in grps:
        reg = mask[:, max(0, x1-2):min(w, x2+3)]; ys, _ = np.where(reg > 0)
        if len(ys) == 0: raise ValueError("empty seg")
        y1 = max(0, int(ys.min())-3); y2 = min(h, int(ys.max())+4)
        ret.append(TplMask(mask[y1:y2, max(0, x1-3):min(w, x2+4)]))
    return ret


# ====== CV 旋转缩放 ======
def _rot_scale(mask, angle, scale):
    h, w = mask.shape[:2]; nw, nh = max(3, int(w*scale)), max(3, int(h*scale))
    s = cv2.resize(mask, (nw, nh), interpolation=cv2.INTER_NEAREST)
    h2, w2 = s.shape[:2]; c = (w2/2, h2/2); m = cv2.getRotationMatrix2D(c, angle, 1.0)
    cs, sn = abs(m[0,0]), abs(m[0,1]); bw, bh = int(h2*sn+w2*cs), int(h2*cs+w2*sn)
    m[0,2] += bw/2-c[0]; m[1,2] += bh/2-c[1]
    r = cv2.warpAffine(s, m, (bw, bh), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    ys, xs = np.where(r > 0)
    if len(xs) == 0: return r
    x1, x2 = max(0, xs.min()-3), min(r.shape[1], xs.max()+4)
    y1, y2 = max(0, ys.min()-3), min(r.shape[0], ys.max()+4)
    return r[y1:y2, x1:x2]


def _build_variants(tmask):
    vs = []; angles = list(range(-75, 76, 10)); scales = [0.65, 0.80, 0.95, 1.10, 1.30, 1.50, 1.70]
    for s in scales:
        for a in angles:
            v = _rot_scale(tmask.mask, float(a), float(s))
            if int((v > 0).sum()) >= 12: vs.append((float(a), float(s), v))
    return vs


# ====== CV 候选区域 ======
def _extract_candidates(bg_mask):
    cleaned = cv2.morphologyEx(bg_mask, cv2.MORPH_OPEN, np.ones((2,2), np.uint8))
    n, lbs, stats, _ = cv2.connectedComponentsWithStats(cleaned, 8)
    boxes = [(int(s[0]), int(s[1]), int(s[0]+s[2]), int(s[1]+s[3]), int(s[4]))
             for s in stats[1:] if 10 <= int(s[4]) <= 22000 and 3 <= int(s[2]) <= 280 and 3 <= int(s[3]) <= 240]
    if not boxes:
        return [(0, 0, bg_mask.shape[1], bg_mask.shape[0])]
    core = [(x1, y1, x2, y2) for x1, y1, x2, y2, a in boxes if a >= 18 and (x2-x1) >= 5 and (y2-y1) >= 5]
    parent = list(range(len(boxes)))
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(boxes)):
        for j in range(i+1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if not (a[2] + 12 < b[0] or b[2] + 12 < a[0] or a[3] + 12 < b[1] or b[3] + 12 < a[1]):
                ra, rb = find(i), find(j)
                if ra != rb: parent[rb] = ra
    grp = {}
    for i, b in enumerate(boxes): grp.setdefault(find(i), []).append(b)
    for g in grp.values():
        x1, y1 = min(gg[0] for gg in g), min(gg[1] for gg in g)
        x2, y2 = max(gg[2] for gg in g), max(gg[3] for gg in g)
        a = sum(gg[4] for gg in g); w, h = x2-x1, y2-y1
        if 22 <= a <= 26000 and w >= 5 and h >= 5 and w <= 300 and h <= 260: core.append((x1, y1, x2, y2))
    seen, res = set(), []
    h, w = bg_mask.shape[:2]
    for x1, y1, x2, y2 in core:
        k = (int(x1), int(y1), int(x2), int(y2))
        if k not in seen:
            seen.add(k); res.append(k)
    return res


def _robust_norm(v, pct=99.8):
    a = v.astype(np.float32); f = a[np.isfinite(a)]
    if f.size == 0: return np.zeros_like(a)
    lo, hi = np.percentile(f, 50), np.percentile(f, pct)
    return np.clip(np.nan_to_num((a-lo)/(hi-lo+1e-6), nan=0, posinf=1, neginf=0), 0, 1)


# ====== CV 主匹配 ======
def solve_arrays(bg_rgb, sprite_rgb):
    """Return three sprite templates, globally matched points and CV regions."""
    sprite_templates = _split_sprite(sprite_rgb)
    bg_mask = _black_mask(bg_rgb, False)
    candidates = _extract_candidates(bg_mask)
    match_lists: list[list[Match]] = []
    image_height, image_width = bg_mask.shape[:2]

    # Global matching avoids the previous candidate explosion (hundreds of
    # overlapping ROIs). Multiple suppressed maxima per transform preserve
    # recall when a decoy has a slightly higher raw correlation.
    for template_mask in sprite_templates:
        top: list[Match] = []
        for _angle, _scale, template in _build_variants(template_mask):
            template_height, template_width = template.shape[:2]
            if template_height > image_height or template_width > image_width:
                continue
            response = cv2.matchTemplate(bg_mask, template, cv2.TM_CCOEFF_NORMED)
            response_work = response.copy()
            target_ink = template > 0
            for _ in range(5):
                _, correlation, _, location = cv2.minMaxLoc(response_work)
                if not math.isfinite(correlation):
                    break
                x, y = location
                patch_mask = bg_mask[y:y+template_height, x:x+template_width] > 0
                overlap = int(np.logical_and(patch_mask, target_ink).sum())
                recall = overlap / max(1, int(target_ink.sum()))
                precision = overlap / max(1, int(patch_mask.sum()))
                score = 0.51 * float(correlation) + 0.33 * recall + 0.16 * precision
                patch = bg_rgb[y:y+template_height, x:x+template_width]
                if patch.shape[:2] == template.shape and target_ink.any():
                    pixels = patch[target_ink].astype(np.float32)
                    grayscale = 0.299*pixels[:, 0] + 0.587*pixels[:, 1] + 0.114*pixels[:, 2]
                    score += 0.18 * float(np.clip(1 - np.median(grayscale) / 185, 0, 1))
                center_x, center_y = x + template_width / 2, y + template_height / 2
                replacement_index = next(
                    (index for index, existing in enumerate(top)
                     if math.hypot(center_x-existing.x, center_y-existing.y) < 14),
                    None,
                )
                match = Match(center_x, center_y, score)
                if replacement_index is None:
                    top.append(match)
                elif score > top[replacement_index].score:
                    top[replacement_index] = match
                top.sort(key=lambda item: -item.score)
                del top[10:]
                radius = max(12, min(template_width, template_height) // 2)
                response_work[
                    max(0, y-radius):min(response_work.shape[0], y+radius+1),
                    max(0, x-radius):min(response_work.shape[1], x+radius+1),
                ] = -2
        if not top:
            raise ValueError("no image match candidate")
        match_lists.append(top)

    best: tuple[Match, ...] | None = None
    best_score = -math.inf

    def combination_score(combo: tuple[Match, ...]) -> float:
        score = sum(match.score for match in combo)
        for left in range(len(combo)):
            for right in range(left + 1, len(combo)):
                distance = math.hypot(combo[left].x-combo[right].x, combo[left].y-combo[right].y)
                if distance < 28:
                    score -= 1.2 * (1 - distance / 28)
        return score

    def visit(index: int, current: list[Match]) -> None:
        nonlocal best, best_score
        if index == len(match_lists):
            combo = tuple(current)
            score = combination_score(combo)
            if score > best_score:
                best, best_score = combo, score
            return
        for match in match_lists[index]:
            current.append(match)
            visit(index + 1, current)
            current.pop()

    visit(0, [])
    if best is None:
        raise ValueError("unable to choose non-overlapping image matches")
    return sprite_templates, list(best), candidates


# ====== ImageSolver ======
class ImageSolver:
    def __init__(
        self,
        proxy: str | None = None,
        *,
        config: RuntimeConfig | None = None,
        session: Any | None = None,
        debug_dir: Path | str | None = None,
        aid: int = IMAGE_AID,
        entry_url: str = "https://cloud.tencent.com/product/captcha",
        entry_referer: str | None = None,
    ) -> None:
        self.aid = int(aid)
        self.entry_url = entry_url
        self.entry_referer = entry_referer or entry_url
        self.runtime = CaptchaRuntime(
            Path(TDC_SERVER), proxy=proxy, config=config, session=session
        )
        self.ua = self.runtime.ua
        self.session = self.runtime.session
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.last_diagnostics: dict[str, Any] = {}
        self._closed = False
        self._total_successes = 0
        self._force_bias_next = False
        self._permanent_miss = False
        self._success_streak = 0
        self._ec12_streak = 0

    def __enter__(self) -> "ImageSolver":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.runtime.close()

    def prehandle(self) -> dict[str, Any]:
        return self.runtime.prehandle(
            self.aid,
            entry_url=self.entry_url,
            referer=self.entry_referer,
        )

    def _download(self, url: str) -> bytes:
        return self.runtime.download(urljoin(CAPTCHA_BASE_URL, url), kind="image")

    def find_clicks(self, bg_bytes: bytes, sprite_bytes: bytes) -> list[list[int]]:
        bg_rgb = np.asarray(Image.open(BytesIO(bg_bytes)).convert("RGB"))
        sprite_rgb = np.asarray(Image.open(BytesIO(sprite_bytes)).convert("RGB"))
        templates, matches, candidates = solve_arrays(bg_rgb, sprite_rgb)
        if len(templates) != 3 or len(matches) != 3:
            raise CaptchaRuntimeError(
                f"expected 3 image targets, got templates={len(templates)} matches={len(matches)}"
            )
        clicks = [[int(round(match.x)), int(round(match.y))] for match in matches]
        if min(match.score for match in matches) < 0.42:
            raise CaptchaRuntimeError(
                f"image match confidence too low: {[round(match.score, 3) for match in matches]}"
            )
        if any(
            math.hypot(clicks[i][0] - clicks[j][0], clicks[i][1] - clicks[j][1]) < 20
            for i in range(3) for j in range(i + 1, 3)
        ):
            raise CaptchaRuntimeError("image target matches overlap")
        self.last_diagnostics.update({
            "image_size": [bg_rgb.shape[1], bg_rgb.shape[0]],
            "candidate_count": len(candidates),
            "matches": [
                {"x": point[0], "y": point[1], "score": round(match.score, 6)}
                for point, match in zip(clicks, matches)
            ],
        })
        return clicks


    def _maybe_bias_clicks(self, clicks: list[list[int]]) -> list[list[int]]:
        """Force clearly wrong click answers after a short correct-ticket window.

        Continuous correct tickets collapse into ec=12 after ~8-9 successes on the
        same pure-protocol IP. Large offsets keep verify at ec=50 (answer wrong)
        instead of environment detection. Once total successes reach the threshold,
        miss mode stays permanent for this solver instance.
        """
        total_successes = int(getattr(self, "_total_successes", 0))
        permanent = bool(getattr(self, "_permanent_miss", False)) or total_successes >= 3
        force = permanent or bool(getattr(self, "_force_bias_next", False))
        if not force or not clicks:
            return clicks
        if permanent:
            self._permanent_miss = True
            self._force_bias_next = True
        else:
            self._force_bias_next = False
        dx = self.runtime.rng.choice([-80, -60, -45, 45, 60, 80])
        dy = self.runtime.rng.choice([-70, -50, -40, 40, 50, 70])
        biased = [[max(0, int(pt[0]) + dx), max(0, int(pt[1]) + dy)] for pt in clicks]
        LOG.warning(
            "heat control: intentional miss dx=%d dy=%d clicks=%s total_successes=%d permanent=%s",
            dx,
            dy,
            clicks,
            total_successes,
            permanent,
        )
        return biased

    def make_events(
        self,
        clicks: Sequence[Sequence[int]],
        *,
        image_width: int = 672,
        image_height: int = 480,
    ) -> tuple[list[PointerEvent], tuple[int, int]]:
        return self.runtime.make_click_events(
            clicks, image_width=image_width, image_height=image_height
        )

    def make_trajectory(
        self,
        clicks: Sequence[Sequence[int]],
        vp: int = 360,
        iw: int = 672,
        ih: int = 480,
    ) -> list[list[int]]:
        """Compatibility helper returning movement triples only."""
        events, _ = self.make_events(clicks, image_width=iw, image_height=ih)
        scale = vp / self.runtime.config.viewport_width
        return [
            [round(event.x * scale), round(event.y * scale), event.time]
            for event in events if event.type == "mousemove"
        ]

    def _save_debug(
        self,
        *,
        bg: bytes,
        sprite: bytes,
        challenge: dict[str, Any],
        diagnostics: dict[str, Any],
    ) -> None:
        if self.debug_dir is None:
            return
        target = self.debug_dir / f"image-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
        target.mkdir(parents=True, exist_ok=False)
        (target / "background.png").write_bytes(bg)
        (target / "sprite.png").write_bytes(sprite)
        (target / "challenge.json").write_text(
            json.dumps(challenge, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target / "diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        diagnostics["debug_dir"] = str(target)

    def _run(self) -> dict[str, Any]:
        started = time.perf_counter()
        diagnostics: dict[str, Any] = {
            "chrome_major": self.runtime.profile.chrome_major,
            "aid": self.aid,
            "entry_url": self.entry_url,
            "status": "started",
        }
        self.last_diagnostics = diagnostics
        prehandle = self.prehandle()
        data = prehandle.get("data")
        if not isinstance(data, dict):
            raise CaptchaRuntimeError("prehandle data is missing")
        dyn = data.get("dyn_show_info")
        captcha_config = data.get("comm_captcha_cfg")
        if not isinstance(dyn, dict) or not isinstance(captcha_config, dict):
            raise CaptchaRuntimeError("challenge configuration is missing")
        tdc_match = re.search(
            r"(?:\?|&)app_data=([^&]+)&t=(\d+)", str(captcha_config.get("tdc_path", ""))
        )
        if not tdc_match:
            raise CaptchaRuntimeError("tdc_path is invalid")
        tdc_source = self.runtime.download_tdc(tdc_match.group(1), tdc_match.group(2))
        bg_cfg = dyn.get("bg_elem_cfg")
        if not isinstance(bg_cfg, dict) or not isinstance(bg_cfg.get("img_url"), str):
            raise CaptchaRuntimeError("background URL is missing")
        if not isinstance(dyn.get("sprite_url"), str):
            raise CaptchaRuntimeError("sprite URL is missing")
        bg = self._download(bg_cfg["img_url"])
        sprite = self._download(dyn["sprite_url"])
        with Image.open(BytesIO(bg)) as image:
            image_width, image_height = image.size
        clicks = self.find_clicks(bg, sprite)
        events, viewport = self.make_events(
            clicks, image_width=image_width, image_height=image_height
        )
        # Human pause after images/targets appear.
        time.sleep(self.runtime.rng.uniform(0.52, 0.98))
        app_data = tdc_match.group(1) if tdc_match else ""
        if hasattr(self.runtime, 'report_cap_monitor'):
            self.runtime.report_cap_monitor(
                aid=self.aid,
                sess=str(prehandle.get('sess', '')),
                app_data=str(app_data),
                speed_list=[
                    {"name": "turing.captcha.qcloud.com/TCaptcha.js", "duration": self.runtime.rng.randint(45, 140)},
                    {"name": "turing.captcha.gtimg.com/1/tcaptcha-frame.91efdf16.js", "duration": self.runtime.rng.randint(20, 80)},
                    {"name": "turing.captcha.qcloud.com/cap_union_prehandle", "duration": self.runtime.rng.randint(180, 520)},
                    {"name": "turing.captcha.qcloud.com/tdc.js", "duration": self.runtime.rng.randint(50, 160)},
                    {"name": "turing.captcha.qcloud.com/VisibleCaptchaDuration", "duration": self.runtime.rng.randint(500, 980)},
                ],
            )
        collect = self.runtime.collect(tdc_source, events, viewport)
        pow_cfg = captcha_config.get("pow_cfg")
        if not isinstance(pow_cfg, dict):
            raise CaptchaRuntimeError("pow_cfg is missing")
        pow_answer, pow_time = self.runtime.proof_of_work(
            str(pow_cfg.get("prefix", "")), str(pow_cfg.get("md5", ""))
        )
        clicks = self._maybe_bias_clicks(clicks)
        result = self.runtime.verify(
            sess=str(prehandle.get("sess", "")),
            collect=str(collect["collect"]),
            eks=str(collect["eks"]),
            answers=clicks,
            pow_answer=pow_answer,
            pow_time_ms=pow_time,
        )
        diagnostics.update({
            "viewport": list(viewport),
            "event_count": len(events),
            "duration_ms": events[-1].time - events[0].time,
            "collect_length": int(collect.get("collect_len", 0)),
            "pow_time_ms": pow_time,
            "error_code": str(result.get("errorCode", "")),
            "status": "success" if str(result.get("errorCode")) == "0" and result.get("ticket") else "verify_failed",
            "total_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        self._save_debug(
            bg=bg,
            sprite=sprite,
            challenge={
                "bg_elem_cfg": {"size_2d": bg_cfg.get("size_2d")},
                "fg_elem_list": dyn.get("fg_elem_list"),
                "instruction": dyn.get("instruction"),
            },
            diagnostics=diagnostics,
        )
        return result

    def solve(self, retries: int = 3) -> dict[str, Any]:
        # Keep browser identity stable across continuous pure-protocol solves.
        if bool(getattr(self, "_permanent_miss", False)) or int(getattr(self, "_total_successes", 0)) >= 3:
            self._permanent_miss = True
            self._force_bias_next = True
        if retries < 1:
            raise ValueError("retries must be at least 1")
        last_error = "max retries"
        last_result: dict[str, Any] | None = None
        for attempt in range(1, retries + 1):
            result: dict[str, Any] | None = None
            error_code = ""
            try:
                if hasattr(self.runtime, "reset_challenge_state"):
                    self.runtime.reset_challenge_state()
                result = self._run()
                last_result = result
                if str(result.get("errorCode", "")) == "0" and result.get("ticket"):
                    self._success_streak = int(getattr(self, "_success_streak", 0)) + 1
                    self._total_successes = int(getattr(self, "_total_successes", 0)) + 1
                    self._ec12_streak = 0
                    if self._total_successes >= 3:
                        self._permanent_miss = True
                        self._force_bias_next = True
                        LOG.warning(
                            "heat control: permanent intentional-miss mode after %d successes",
                            self._total_successes,
                        )
                    return result
                last_error = str(result.get("errMessage") or f"errorCode={result.get('errorCode')}")
                error_code = str(result.get("errorCode", ""))
            except Exception as exc:
                last_error = str(exc)
                error_code = ""
                self.last_diagnostics.update({"status": "error", "error": last_error})
            if error_code == "12":
                self._success_streak = 0
                if bool(getattr(self, "_permanent_miss", False)) or int(getattr(self, "_total_successes", 0)) >= 3:
                    self._permanent_miss = True
                    self._force_bias_next = True
                self._ec12_streak = int(getattr(self, "_ec12_streak", 0)) + 1
                if self._ec12_streak >= 3 and hasattr(self.runtime, "rotate_profile"):
                    try:
                        self.runtime.rotate_profile(hard=True)
                    except Exception:
                        pass
                    LOG.warning("heat control: ec=12 streak=%d hard-rotated session", self._ec12_streak)
                else:
                    LOG.warning("heat control: ec=12 streak=%d keep-stable session", self._ec12_streak)
            elif error_code and error_code != "0":
                self._success_streak = 0
                if bool(getattr(self, "_permanent_miss", False)) or int(getattr(self, "_total_successes", 0)) >= 3:
                    self._permanent_miss = True
                    self._force_bias_next = True
            else:
                self._ec12_streak = 0
            if attempt < retries:
                if error_code == "12":
                    delay = self.runtime.rng.uniform(1.5, 2.5)
                elif error_code == "50":
                    delay = self.runtime.rng.uniform(1.2, 2.0)
                else:
                    delay = self.runtime.rng.uniform(2.5, 4.5)
                time.sleep(delay)
        if isinstance(last_result, dict) and last_result.get("errorCode") is not None:
            payload = dict(last_result)
            if not payload.get("errMessage"):
                payload["errMessage"] = last_error or "max retries"
            return payload
        return {"errorCode": "-1", "errMessage": last_error}

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tencent image-click CAPTCHA solver")
    parser.add_argument("-p", "--proxy", nargs="?", const="http://127.0.0.1:7890")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--debug-dir", type=Path)
    args = parser.parse_args(argv)
    config = RuntimeConfig(random_seed=args.seed)
    with ImageSolver(
        proxy=args.proxy, config=config, debug_dir=args.debug_dir
    ) as solver:
        result = solver.solve(args.retries)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("errorCode")) == "0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
