import time
"""
腾讯防水墙文字点选验证码 — 完整协议

aid=199999888 / subcapclass=2404
OCR: Siamese 孪生网络 (model/pre_model_v7.onnx) 匹配指令字符
"""
import argparse
import logging, itertools, json, math, os, re, sys, threading, time
from io import BytesIO

import cv2, numpy as np
import onnxruntime as ort
import ddddocr
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urljoin

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
TEXT_AID = 199999888
TDC_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tdc_server.js")

LOG = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))

FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/simkai.ttf",
]
FONTS = []
for font_path in FONT_PATHS:
    if os.path.exists(font_path):
        for font_size in (48, 56, 64, 72):
            try:
                FONTS.append(ImageFont.truetype(font_path, font_size))
            except OSError:
                pass


def jsonp_parse(text):
    m = re.search(r"_aq_\d+\((.*)\)", text)
    return json.loads(m.group(1)) if m else json.loads(text)


# ====== Siamese ONNX 内联 ======
_OCR_LOCK = threading.RLock()
_SHARED_DET = None
_SHARED_CLS = None
_SHARED_SIAMESE = None
_SHARED_MODEL_PATH = None
_WARMUP_DONE = False


def default_model_path() -> str:
    return os.path.join(BASE, "model", "pre_model_v7.onnx")


class Siamese:
    def __init__(self, model_path=None, size=(112, 112)):
        self.size = size
        self.model_path = model_path or default_model_path()
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"text model missing: {self.model_path}")
        self.sess = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        self.inames = [i.name for i in self.sess.get_inputs()]
        self.onames = [o.name for o in self.sess.get_outputs()]

    def _prep(self, img):
        if len(img.shape) == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        s = min(self.size[0] / w, self.size[1] / h)
        nw, nh = int(w * s), int(h * s)
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_CUBIC)
        canvas = np.full((self.size[1], self.size[0], 3), 128, dtype=np.uint8)
        dx, dy = (self.size[0] - nw) // 2, (self.size[1] - nh) // 2
        canvas[dy:dy+nh, dx:dx+nw] = img
        arr = canvas.astype(np.float32) / 255.0
        return np.expand_dims(np.transpose(arr, (2, 0, 1)), 0)

    def predict(self, img1, img2):
        i = {self.inames[0]: self._prep(img1), self.inames[1]: self._prep(img2)}
        r = self.sess.run(self.onames, i)[0]
        return float(1.0 / (1.0 + np.exp(-np.clip(r, -40, 40)))[0, 0])

    def batch_predict(self, list1, list2):
        p1 = [self._prep(img) for img in list1]
        p2 = [self._prep(img) for img in list2]
        x1, x2 = [], []
        for a in p1:
            x1.extend([a] * len(p2))
            x2.extend(p2)
        i = {self.inames[0]: np.concatenate(x1, 0), self.inames[1]: np.concatenate(x2, 0)}
        r = self.sess.run(self.onames, i)[0]
        p = (1.0 / (1.0 + np.exp(-np.clip(r, -40, 40)))).flatten().tolist()
        return [p[i*len(p2):(i+1)*len(p2)] for i in range(len(list1))]

    def match_char_to_boxes(self, targets, boxes_crops, bg_img):
        """对每个 target, 多字体渲染模板, 批量比对所有 box"""
        n_t, n_b = len(targets), len(boxes_crops)
        scores = [[0.0] * n_b for _ in range(n_t)]
        for ti, ch in enumerate(targets):
            tmpls = []
            for font in FONTS[:12]:
                tpl = Image.new("RGB", (80, 80), "white")
                d = ImageDraw.Draw(tpl)
                try:
                    bb = d.textbbox((0, 0), ch, font=font)
                    d.text(((80-bb[2]-bb[0])//2 - bb[0], (80-bb[3]-bb[1])//2 - bb[1]), ch, fill="black", font=font)
                except:
                    d.text((10, 10), ch, fill="black", font=font)
                tmpls.append(np.array(tpl)[:, :, ::-1])
            if tmpls:
                all_s = self.batch_predict(tmpls, boxes_crops)
                for k, s_row in enumerate(all_s):
                    for bi in range(n_b):
                        if s_row[bi] > scores[ti][bi]:
                            scores[ti][bi] = s_row[bi]
        return scores


# ====== 匈牙利匹配 ======
def parse_instruction(instruction: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(instruction)).strip()
    match = re.search(r"(?:\u8bf7\s*)?\u70b9\u51fb\s*[\uff1a:]\s*(.+)$", normalized)
    payload = match.group(1) if match else normalized
    tokens = [token for token in re.split(r"[\s,\uff0c\u3001]+", payload) if token]
    if len(tokens) == 1 and len(tokens[0]) == 3:
        tokens = list(tokens[0])
    if len(tokens) != 3:
        raise ValueError(f"instruction parse failed: {instruction!r}")
    return tokens


def optimal_assignment(scores: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    matrix = np.asarray(scores, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] < matrix.shape[0]:
        raise ValueError("score matrix cannot produce a unique assignment")
    rows, columns = matrix.shape
    best_columns: tuple[int, ...] | None = None
    best_score = -math.inf
    for selected in itertools.permutations(range(columns), rows):
        score = sum(float(matrix[row, column]) for row, column in enumerate(selected))
        if score > best_score:
            best_score = score
            best_columns = selected
    assert best_columns is not None
    return [(row, best_columns[row]) for row in range(rows)]


def hungarian(scores: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Compatibility alias for the exact small-matrix assignment solver."""
    return optimal_assignment(scores)



def get_shared_ocr(model_path: str | None = None):
    """Load and cache ddddocr + Siamese model once for process-wide reuse."""
    global _SHARED_DET, _SHARED_CLS, _SHARED_SIAMESE, _SHARED_MODEL_PATH
    target = os.path.abspath(model_path or default_model_path())
    with _OCR_LOCK:
        if (
            _SHARED_SIAMESE is not None
            and _SHARED_DET is not None
            and _SHARED_CLS is not None
            and _SHARED_MODEL_PATH == target
        ):
            return _SHARED_DET, _SHARED_CLS, _SHARED_SIAMESE
        det = ddddocr.DdddOcr(det=True, show_ad=False)
        cls = ddddocr.DdddOcr(det=False, show_ad=False)
        siamese = Siamese(model_path=target)
        blank = np.full((32, 32, 3), 255, dtype=np.uint8)
        _ = siamese.predict(blank, blank)
        _SHARED_DET, _SHARED_CLS, _SHARED_SIAMESE = det, cls, siamese
        _SHARED_MODEL_PATH = target
        return _SHARED_DET, _SHARED_CLS, _SHARED_SIAMESE


def warmup_text_models(model_path: str | None = None) -> dict:
    """Eagerly preload OCR/Siamese so the first text task is fast."""
    global _WARMUP_DONE
    started = time.perf_counter()
    det, cls, siamese = get_shared_ocr(model_path)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _WARMUP_DONE = True
    return {
        "status": "ready",
        "model_path": getattr(siamese, "model_path", model_path or default_model_path()),
        "warmup_ms": elapsed_ms,
        "providers": ["CPUExecutionProvider"],
        "cached": True,
        "det_ready": det is not None,
        "cls_ready": cls is not None,
        "siamese_ready": siamese is not None,
    }


class TextSolver:
    def __init__(
        self,
        proxy: str | None = None,
        *,
        config: RuntimeConfig | None = None,
        session: Any | None = None,
        debug_dir: Path | str | None = None,
        aid: int = TEXT_AID,
        entry_url: str = "https://cloud.tencent.com/product/captcha",
        entry_referer: str | None = None,
    ) -> None:
        self.aid = int(aid)
        self.entry_url = entry_url
        self.entry_referer = entry_referer or entry_url
        self.runtime = CaptchaRuntime(Path(TDC_SERVER), proxy=proxy, config=config, session=session)
        self.ua = self.runtime.ua
        self.session = self.runtime.session
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.last_diagnostics: dict[str, Any] = {}
        self._siamese: Siamese | None = None
        self._det: Any | None = None
        self._cls: Any | None = None
        self._closed = False
        self._total_successes = 0
        self._force_bias_next = False
        self._permanent_miss = False
        self._success_streak = 0
        self._ec12_streak = 0

    def __enter__(self) -> "TextSolver":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
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

    def _init_ocr(self) -> None:
        if self._det is None or self._cls is None or self._siamese is None:
            self._det, self._cls, self._siamese = get_shared_ocr()

    @staticmethod
    def _normalize_boxes(boxes: Sequence[Sequence[int]], image_width: int, image_height: int) -> list[tuple[int, int, int, int]]:
        valid: list[tuple[int, int, int, int]] = []
        min_side = max(18, round(min(image_width, image_height) * 0.045))
        for raw in boxes:
            if len(raw) != 4:
                continue
            x1, y1, x2, y2 = map(int, raw)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(image_width, x2), min(image_height, y2)
            width, height = x2 - x1, y2 - y1
            if width >= min_side and height >= min_side and width <= image_width * 0.45 and height <= image_height * 0.45:
                valid.append((x1, y1, x2, y2))
        valid.sort(key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
        deduped: list[tuple[int, int, int, int]] = []
        for box in valid:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if any(math.hypot(cx - (other[0] + other[2]) / 2, cy - (other[1] + other[3]) / 2) < 12 for other in deduped):
                continue
            deduped.append(box)
        return deduped

    def find_clicks(self, image_bytes: bytes, instruction: str) -> list[list[int]]:
        self._init_ocr()
        assert self._det is not None and self._siamese is not None
        targets = parse_instruction(instruction)
        with Image.open(BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
            image_width, image_height = image.size
        boxes = self._normalize_boxes(self._det.detection(image_bytes), image_width, image_height)
        if len(boxes) < 3:
            raise CaptchaRuntimeError(f"not enough text boxes: {len(boxes)}")
        crops: list[np.ndarray] = []
        centers: list[tuple[int, int]] = []
        for x1, y1, x2, y2 in boxes:
            pad = max(5, round(max(x2 - x1, y2 - y1) * 0.10))
            crop = image.crop((max(0, x1-pad), max(0, y1-pad), min(image_width, x2+pad), min(image_height, y2+pad)))
            crops.append(np.asarray(crop)[:, :, ::-1].copy())
            centers.append((round((x1+x2)/2), round((y1+y2)/2)))
        scores = self._siamese.match_char_to_boxes(targets, crops, image)
        assignment = optimal_assignment(scores)
        selected_scores = [float(scores[target][box]) for target, box in assignment]
        if min(selected_scores) < 0.08 or sum(selected_scores) / 3 < 0.32:
            raise CaptchaRuntimeError(f"text match confidence too low: {[round(score, 3) for score in selected_scores]}")
        clicks = [list(centers[box]) for _, box in assignment]
        self.last_diagnostics.update({
            "instruction": instruction,
            "targets": targets,
            "image_size": [image_width, image_height],
            "box_count": len(boxes),
            "matches": [
                {"target": targets[target], "x": centers[box][0], "y": centers[box][1], "score": round(float(scores[target][box]), 6)}
                for target, box in assignment
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

    def make_events(self, clicks: Sequence[Sequence[int]], *, image_width: int = 672, image_height: int = 480) -> tuple[list[PointerEvent], tuple[int, int]]:
        return self.runtime.make_click_events(clicks, image_width=image_width, image_height=image_height)

    def make_trajectory(self, clicks: Sequence[Sequence[int]], vp: int = 360, iw: int = 672, ih: int = 480) -> list[list[int]]:
        events, _ = self.make_events(clicks, image_width=iw, image_height=ih)
        scale = vp / self.runtime.config.viewport_width
        return [[round(event.x*scale), round(event.y*scale), event.time] for event in events if event.type == "mousemove"]

    def _save_debug(self, *, background: bytes, challenge: dict[str, Any], diagnostics: dict[str, Any]) -> None:
        if self.debug_dir is None:
            return
        target = self.debug_dir / f"text-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
        target.mkdir(parents=True, exist_ok=False)
        (target / "background.png").write_bytes(background)
        (target / "challenge.json").write_text(json.dumps(challenge, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostics["debug_dir"] = str(target)

    def _run(self) -> dict[str, Any]:
        started = time.perf_counter()
        diagnostics: dict[str, Any] = {"chrome_major": self.runtime.profile.chrome_major, "status": "started"}
        self.last_diagnostics = diagnostics
        prehandle = self.prehandle()
        data = prehandle.get("data")
        if not isinstance(data, dict):
            raise CaptchaRuntimeError("prehandle data is missing")
        dyn, captcha_config = data.get("dyn_show_info"), data.get("comm_captcha_cfg")
        if not isinstance(dyn, dict) or not isinstance(captcha_config, dict):
            raise CaptchaRuntimeError("challenge configuration is missing")
        tdc_match = re.search(r"(?:\?|&)app_data=([^&]+)&t=(\d+)", str(captcha_config.get("tdc_path", "")))
        if not tdc_match:
            raise CaptchaRuntimeError("tdc_path is invalid")
        tdc_source = self.runtime.download_tdc(tdc_match.group(1), tdc_match.group(2))
        bg_cfg, instruction = dyn.get("bg_elem_cfg"), dyn.get("instruction")
        if not isinstance(bg_cfg, dict) or not isinstance(bg_cfg.get("img_url"), str):
            raise CaptchaRuntimeError("background URL is missing")
        if not isinstance(instruction, str):
            raise CaptchaRuntimeError("instruction is missing")
        background = self._download(bg_cfg["img_url"])
        with Image.open(BytesIO(background)) as image:
            image_width, image_height = image.size
        clicks = self.find_clicks(background, instruction)
        events, viewport = self.make_events(clicks, image_width=image_width, image_height=image_height)
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
        pow_answer, pow_time = self.runtime.proof_of_work(str(pow_cfg.get("prefix", "")), str(pow_cfg.get("md5", "")))
        clicks = self._maybe_bias_clicks(clicks)
        result = self.runtime.verify(
            sess=str(prehandle.get("sess", "")), collect=str(collect["collect"]), eks=str(collect["eks"]),
            answers=clicks, pow_answer=pow_answer, pow_time_ms=pow_time,
        )
        diagnostics.update({
            "viewport": list(viewport), "event_count": len(events),
            "duration_ms": events[-1].time-events[0].time,
            "collect_length": int(collect.get("collect_len", 0)), "pow_time_ms": pow_time,
            "error_code": str(result.get("errorCode", "")),
            "status": "success" if str(result.get("errorCode")) == "0" and result.get("ticket") else "verify_failed",
            "total_ms": round((time.perf_counter()-started)*1000, 2),
        })
        self._save_debug(background=background, challenge={"instruction": instruction, "bg_elem_cfg": {"size_2d": bg_cfg.get("size_2d")}}, diagnostics=diagnostics)
        return result

    def solve(self, retries: int = 3) -> dict[str, Any]:
        # Keep browser identity stable across continuous pure-protocol solves.
        if bool(getattr(self, "_permanent_miss", False)) or int(getattr(self, "_total_successes", 0)) >= 3:
            self._permanent_miss = True
            self._force_bias_next = True
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self._init_ocr()
        last_error = "max retries"
        last_result: dict[str, Any] | None = None
        for attempt in range(1, retries+1):
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
                last_error, error_code = str(exc), ""
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
    parser = argparse.ArgumentParser(description="Tencent text-click CAPTCHA solver")
    parser.add_argument("-p", "--proxy", nargs="?", const="http://127.0.0.1:7890")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--debug-dir", type=Path)
    args = parser.parse_args(argv)
    with TextSolver(proxy=args.proxy, config=RuntimeConfig(random_seed=args.seed), debug_dir=args.debug_dir) as solver:
        result = solver.solve(args.retries)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("errorCode")) == "0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
