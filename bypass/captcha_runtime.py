"""Shared browser transport, fingerprint and human input runtime for CAPTCHA demos."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import queue
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import requests

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.errors import RequestsError as CurlRequestsError
except ImportError:  # pragma: no cover
    curl_requests = None
    CurlRequestsError = OSError

REQUEST_ERRORS = (requests.RequestException, CurlRequestsError)
PREHANDLE_URL = "https://turing.captcha.qcloud.com/cap_union_prehandle"
TDC_URL = "https://turing.captcha.qcloud.com/tdc.js"
VERIFY_URL = "https://turing.captcha.qcloud.com/cap_union_new_verify"
CAP_MONITOR_URL = "https://turing.captcha.qcloud.com/cap_monitor"
CAPTCHA_BASE_URL = "https://turing.captcha.qcloud.com/"
CAPTCHA_REFERER = "https://turing.captcha.gtimg.com/"
ENTRY_URL = "https://cloud.tencent.com/product/captcha"
ENTRY_REFERER = "https://cloud.tencent.com/"
CHROME_MAJORS = (146,)


class CaptchaRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    chrome_major: int
    user_agent: str
    sec_ch_ua: str
    language: str
    languages: tuple[str, ...]
    platform: str
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    screen_width: int
    screen_height: int
    avail_height: int
    window_width: int
    window_height: int
    outer_width: int
    outer_height: int
    device_pixel_ratio: float
    timezone: str
    webgl_vendor: str
    webgl_renderer: str
    canvas_seed: int

    def tdc_payload(self) -> dict[str, Any]:
        return {
            "chromeMajor": self.chrome_major,
            "language": self.language,
            "languages": list(self.languages),
            "platform": self.platform,
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory,
            "maxTouchPoints": self.max_touch_points,
            "doNotTrack": None,
            "pdfViewerEnabled": True,
            "screen": {
                "width": self.screen_width,
                "height": self.screen_height,
                "availWidth": self.screen_width,
                "availHeight": self.avail_height,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": self.window_width,
                "innerHeight": self.window_height,
                "outerWidth": self.outer_width,
                "outerHeight": self.outer_height,
                "devicePixelRatio": self.device_pixel_ratio,
                "screenX": 0,
                "screenY": 0,
            },
            "timezone": self.timezone,
            "webglVendor": self.webgl_vendor,
            "webglRenderer": self.webgl_renderer,
            "canvasSeed": self.canvas_seed,
        }


def make_browser_profile(rng: random.Random) -> BrowserProfile:
    major = 146
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    sec_ch_ua = f'"Chromium";v="{major}", "Not-A.Brand";v="24", "Google Chrome";v="{major}"'
    return BrowserProfile(
        chrome_major=major,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        language="zh-CN",
        languages=("zh-CN",),
        platform="Win32",
        hardware_concurrency=8,
        device_memory=8,
        max_touch_points=10,
        screen_width=1920,
        screen_height=1080,
        avail_height=1032,
        window_width=1440,
        window_height=900,
        outer_width=1456,
        outer_height=988,
        device_pixel_ratio=1.0,
        timezone="Asia/Shanghai",
        webgl_vendor="Google Inc. (NVIDIA)",
        webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        canvas_seed=rng.randrange(1, 2**31),
    )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    random_seed: int | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    http_retry_interval: float = 5.0
    http_max_attempts: int = 0
    tdc_timeout: float = 5.0
    vm_timeout_ms: int = 2_000
    pow_timeout: float = 10.0
    viewport_width: int = 672
    use_browser_tls: bool = True
    browser_profile: BrowserProfile | None = None
    node_command: str = "node"


@dataclass(frozen=True, slots=True)
class PointerEvent:
    type: str
    x: int
    y: int
    time: int
    button: int = 0
    buttons: int = 0

    def as_payload(self) -> dict[str, int | str]:
        return asdict(self)


class TdcClient:
    def __init__(self, server_path: Path, *, node_command: str, timeout: float, vm_timeout_ms: int):
        self.server_path = Path(server_path)
        self.node_command = node_command
        self.timeout = timeout
        self.vm_timeout_ms = vm_timeout_ms
        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[str | None] | None = None
        self._reader: threading.Thread | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def _reader_loop(self, process: subprocess.Popen[str], output: queue.Queue[str | None]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    def _read(self, timeout: float) -> dict[str, Any]:
        if self._output is None:
            raise CaptchaRuntimeError("TDC worker is not running")
        try:
            line = self._output.get(timeout=timeout)
        except queue.Empty as exc:
            raise CaptchaRuntimeError("TDC worker response timed out") from exc
        if line is None:
            raise CaptchaRuntimeError("TDC worker exited unexpectedly")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptchaRuntimeError("TDC worker returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CaptchaRuntimeError("TDC response must be an object")
        return value

    def _start(self) -> None:
        if self.pid is not None:
            return
        self.close()
        try:
            # Windows: hide node.exe console (20 concurrent captcha = 20 black windows)
            popen_kw: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            process = subprocess.Popen(
                [self.node_command, str(self.server_path)],
                **popen_kw,
            )
        except OSError as exc:
            raise CaptchaRuntimeError(f"failed to start TDC worker: {exc}") from exc
        output: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=self._reader_loop, args=(process, output), daemon=True)
        self._process, self._output, self._reader = process, output, reader
        reader.start()
        try:
            if self._read(self.timeout).get("ready") is not True:
                raise CaptchaRuntimeError("TDC worker did not report ready")
        except Exception:
            self.close()
            raise

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._start()
            assert self._process is not None and self._process.stdin is not None
            self._request_id += 1
            request_id = self._request_id
            message = dict(payload, requestId=request_id, vmTimeoutMs=self.vm_timeout_ms)
            try:
                self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self._process.stdin.flush()
                response = self._read(self.timeout)
            except (OSError, CaptchaRuntimeError):
                self.close()
                raise
            if response.get("requestId") != request_id:
                self.close()
                raise CaptchaRuntimeError("TDC response ID mismatch")
            if response.get("success") is not True:
                self.close()
                raise CaptchaRuntimeError(str(response.get("error", "TDC worker failed")))
            if not response.get("collect") or not response.get("eks"):
                raise CaptchaRuntimeError("TDC response is missing collect or eks")
            return response

    def close(self) -> None:
        process = self._process
        self._process = None
        self._output = None
        self._reader = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        if process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass


class CaptchaRuntime:
    def __init__(
        self,
        tdc_server: Path,
        *,
        proxy: str | None = None,
        config: RuntimeConfig | None = None,
        session: Any | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.rng = random.Random(self.config.random_seed)
        self.profile = self.config.browser_profile or make_browser_profile(self.rng)
        self.ua = self.profile.user_agent
        self.document_referrer = ENTRY_URL
        self.entry_url = ENTRY_URL
        if session is not None:
            self.session = session
        elif self.config.use_browser_tls and curl_requests is not None:
            self.session = curl_requests.Session(impersonate=f"chrome{self.profile.chrome_major}")
        else:
            self.session = requests.Session()
        self._owns_session = session is None
        self.session.headers.update(self._base_headers())
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self._session_warmed = False
        self.tdc = TdcClient(
            tdc_server,
            node_command=self.config.node_command,
            timeout=self.config.tdc_timeout,
            vm_timeout_ms=self.config.vm_timeout_ms,
        )
        self._closed = False
        self._aid_encrypted: str | None = None

    def __enter__(self) -> "CaptchaRuntime":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def timeout(self) -> tuple[float, float]:
        return self.config.connect_timeout, self.config.read_timeout

    def set_aid_encrypted(self, value: str | None) -> None:
        self._aid_encrypted = (value or "").strip() or None

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Accept-Language": (
                f"{self.profile.language},{str(self.profile.language).split('-')[0]};q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
            "sec-ch-ua": self.profile.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    @staticmethod
    def fetch_headers(
        *,
        referer: str,
        accept: str = "*/*",
        mode: str = "cors",
        destination: str = "empty",
        site: str = "cross-site",
    ) -> dict[str, str]:
        return {
            "Accept": accept,
            "Referer": referer,
            "Sec-Fetch-Dest": destination,
            "Sec-Fetch-Mode": mode,
            "Sec-Fetch-Site": site,
        }

    def _can_retry_http(self, attempt: int) -> bool:
        return self.config.http_max_attempts == 0 or attempt < self.config.http_max_attempts

    def get(self, url: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.get(url, timeout=self.timeout, **kwargs)
                status_code = int(getattr(response, "status_code", 200))
                if status_code == 429 or status_code >= 500:
                    raise CaptchaRuntimeError(f"HTTP {status_code}")
                response.raise_for_status()
                self._sanitize_cookies()
                return response
            except REQUEST_ERRORS as error:
                if not self._can_retry_http(attempt):
                    raise CaptchaRuntimeError(f"GET {url} failed: {error}") from error
                time.sleep(self.config.http_retry_interval)
            except CaptchaRuntimeError as error:
                if not self._can_retry_http(attempt):
                    raise CaptchaRuntimeError(f"GET {url} failed: {error}") from error
                time.sleep(self.config.http_retry_interval)

    def prehandle(
        self,
        aid: int,
        *,
        entry_url: str = ENTRY_URL,
        referer: str | None = None,
    ) -> dict[str, Any]:
        self.document_referrer = referer or entry_url
        self.entry_url = entry_url
        self._warm_session(entry_url)
        request_referer = referer or entry_url
        self.document_referrer = request_referer
        time.sleep(self.rng.uniform(0.18, 0.55))
        params: dict[str, Any] = {
            "aid": aid,
            "protocol": "https",
            "accver": 1,
            "showtype": "popup",
            "ua": base64.b64encode(self.ua.encode()).decode(),
            "noheader": 1,
            "fb": 1,
            "aged": 0,
            "enableAged": 0,
            "enableDarkMode": 0,
            "grayscale": 1,
            "clientype": 2,
            "cap_cd": "",
            "uid": "",
            "lang": "zh-cn",
            "entry_url": entry_url,
            "elder_captcha": 0,
            "js": "/tcaptcha-frame.91efdf16.js",
            "login_appid": "",
            "wb": 2,
            "subsid": 1,
            "callback": f"_aq_{self.rng.randint(100000, 999999)}",
            "sess": "",
        }
        if self._aid_encrypted:
            params["aidEncrypted"] = self._aid_encrypted
            params["userLanguage"] = "en"
            params["lang"] = "en"
            params["js"] = "/tgJCap.f0ca357b.js"
            params["wb"] = 1
        response = self.get(
            PREHANDLE_URL,
            headers=self.fetch_headers(
                referer=request_referer, mode="no-cors", destination="script"
            ),
            params=params,
        )
        stripped = response.text.strip()
        if "(" in stripped and stripped.rstrip().endswith((")", ");")):
            payload = stripped[stripped.find("(") + 1 : stripped.rfind(")")]
        else:
            payload = stripped
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CaptchaRuntimeError("prehandle returned invalid JSONP") from exc
        if not isinstance(result, dict) or result.get("state") != 1:
            raise CaptchaRuntimeError(f"prehandle failed: {result!r}")
        return result

    def download(self, url: str, *, kind: str = "image") -> bytes:
        accept = (
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            if kind == "image"
            else "*/*"
        )
        return self.get(
            url,
            headers=self.fetch_headers(
                referer=CAPTCHA_REFERER, accept=accept, mode="no-cors", destination=kind
            ),
        ).content

    def download_tdc(self, app_data: str, timestamp: str) -> str:
        return self.get(
            TDC_URL,
            params={"app_data": app_data, "t": timestamp},
            headers=self.fetch_headers(
                referer=CAPTCHA_REFERER, mode="no-cors", destination="script"
            ),
        ).text

    def _sanitize_cookies(self) -> None:
        try:
            jar = self.session.cookies
        except Exception:
            return
        bad = []
        for cookie in list(jar):
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if not name or name.lower() in {
                "path",
                "domain",
                "expires",
                "max-age",
                "secure",
                "httponly",
                "samesite",
            }:
                bad.append(cookie)
                continue
            if name == "path" or (value in {"/", ""} and name.lower() in {"path", "domain"}):
                bad.append(cookie)
        for cookie in bad:
            try:
                jar.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            except Exception:
                try:
                    jar.clear()
                    break
                except Exception:
                    return

    def soft_refresh_profile(self) -> None:
        current = self.profile
        self.profile = type(current)(
            chrome_major=current.chrome_major,
            user_agent=current.user_agent,
            sec_ch_ua=current.sec_ch_ua,
            language=current.language,
            languages=current.languages,
            platform=current.platform,
            hardware_concurrency=current.hardware_concurrency,
            device_memory=current.device_memory,
            max_touch_points=current.max_touch_points,
            screen_width=current.screen_width,
            screen_height=current.screen_height,
            avail_height=current.avail_height,
            window_width=current.window_width,
            window_height=current.window_height,
            outer_width=current.outer_width,
            outer_height=current.outer_height,
            device_pixel_ratio=current.device_pixel_ratio,
            timezone=current.timezone,
            webgl_vendor=current.webgl_vendor,
            webgl_renderer=current.webgl_renderer,
            canvas_seed=self.rng.randrange(1, 2**31),
        )
        self.ua = self.profile.user_agent

    def reset_challenge_state(self) -> None:
        self.soft_refresh_profile()
        try:
            self.session.cookies.clear()
        except Exception:
            pass

    def rotate_profile(self, *, hard: bool = False) -> None:
        self.rng.seed()
        if not hard and getattr(self, "profile", None) is not None:
            self.soft_refresh_profile()
            return
        self.profile = make_browser_profile(self.rng)
        self.ua = self.profile.user_agent
        headers = {
            "User-Agent": self.ua,
            "Accept-Language": (
                f"{self.profile.language},{str(self.profile.language).split('-')[0]};q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
            "sec-ch-ua": self.profile.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass
            if self.config.use_browser_tls and curl_requests is not None:
                self.session = curl_requests.Session(
                    impersonate=f"chrome{self.profile.chrome_major}"
                )
            else:
                self.session = requests.Session()
        self.session.headers.update(headers)
        try:
            self.session.cookies.clear()
        except Exception:
            pass
        self._session_warmed = False

    def _warm_session(self, entry_url: str | None = None) -> None:
        if getattr(self, "_session_warmed", False):
            return
        page_url = entry_url or getattr(self, "entry_url", None) or ENTRY_URL
        page_headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-User": "?1",
        }
        script_headers = {
            "Accept": "*/*",
            "Referer": page_url,
            "Sec-Fetch-Dest": "script",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
        iframe_headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Referer": page_url,
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
        }
        warm_urls = [
            (page_url, page_headers),
            ("https://turing.captcha.qcloud.com/TCaptcha.js", script_headers),
            ("https://turing.captcha.gtimg.com/1/tcaptcha-frame.91efdf16.js", script_headers),
            ("https://turing.captcha.gtimg.com/1/template/drag_ele.html", iframe_headers),
        ]
        for url, headers in warm_urls:
            try:
                self.session.get(
                    url,
                    headers=headers,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                )
            except Exception:
                pass
            time.sleep(self.rng.uniform(0.05, 0.18))
        self._sanitize_cookies()
        self._session_warmed = True

    def collect(
        self,
        tdc_source: str,
        events: Sequence[PointerEvent],
        viewport: tuple[int, int],
    ) -> dict[str, Any]:
        try:
            self.tdc.close()
        except Exception:
            pass
        return self.tdc.request(
            {
                "tdcSource": tdc_source,
                "events": [event.as_payload() for event in events],
                "ft": "qf_7Pf__H",
                "userAgent": self.ua,
                "fingerprint": self.profile.tdc_payload(),
                "documentReferrer": getattr(self, "entry_url", None) or self.document_referrer,
                "useViewportAsWindow": False,
                "viewport": {"width": viewport[0], "height": viewport[1]},
            }
        )

    def make_click_events(
        self,
        clicks: Sequence[Sequence[float]],
        *,
        image_width: int,
        image_height: int,
    ) -> tuple[list[PointerEvent], tuple[int, int]]:
        if image_width <= 0 or image_height <= 0:
            raise ValueError("image dimensions must be positive")
        scale = self.config.viewport_width / image_width
        viewport = (self.config.viewport_width, max(100, round(image_height * scale)))
        targets = [(round(float(x) * scale), round(float(y) * scale)) for x, y in clicks]
        rng = self.rng
        timestamp = rng.randint(90, 260)
        current_x = rng.randint(-35, 8)
        current_y = rng.randint(45, max(55, viewport[1] - 25))
        events: list[PointerEvent] = []
        for target_index, (target_x, target_y) in enumerate(targets):
            distance = math.hypot(target_x - current_x, target_y - current_y)
            duration = max(280, min(900, int(210 + distance * rng.uniform(1.45, 2.35))))
            steps = max(18, min(70, round(duration / rng.uniform(11.5, 16.5))))
            bend_x = rng.uniform(-0.12, 0.12) * max(distance, 40)
            bend_y = rng.uniform(-0.15, 0.15) * max(distance, 40)
            start_x, start_y = current_x, current_y
            for index in range(1, steps + 1):
                unit = index / steps
                eased = 10 * unit**3 - 15 * unit**4 + 6 * unit**5
                curve = math.sin(math.pi * unit)
                x = start_x + (target_x - start_x) * eased + bend_x * curve
                y = start_y + (target_y - start_y) * eased + bend_y * curve
                if 0.08 < unit < 0.92:
                    x += rng.uniform(-0.45, 0.45)
                    y += rng.uniform(-0.45, 0.45)
                timestamp += rng.randint(8, 19)
                point = PointerEvent("mousemove", round(x), round(y), timestamp)
                if not events or (point.x, point.y) != (events[-1].x, events[-1].y):
                    events.append(point)
            if not events or (events[-1].x, events[-1].y) != (target_x, target_y):
                timestamp += rng.randint(10, 24)
                events.append(PointerEvent("mousemove", target_x, target_y, timestamp))
            timestamp += rng.randint(45, 125)
            events.append(
                PointerEvent("mousedown", target_x, target_y, timestamp, button=0, buttons=1)
            )
            timestamp += rng.randint(55, 135)
            events.append(
                PointerEvent("mouseup", target_x, target_y, timestamp, button=0, buttons=0)
            )
            timestamp += rng.randint(1, 12)
            events.append(
                PointerEvent("click", target_x, target_y, timestamp, button=0, buttons=0)
            )
            current_x, current_y = target_x, target_y
            if target_index + 1 < len(targets):
                timestamp += rng.randint(170, 430)
        return events, viewport

    def proof_of_work(self, prefix: str, target_hash: str) -> tuple[str, int]:
        if not prefix or len(target_hash) < 6:
            raise CaptchaRuntimeError("invalid proof-of-work configuration")
        target = target_hash[:6]
        started = time.perf_counter()
        nonce = 0
        while True:
            candidate = f"{prefix}{nonce}"
            if hashlib.md5(candidate.encode()).hexdigest().startswith(target):
                return candidate, max(1, round((time.perf_counter() - started) * 1000))
            nonce += 1
            if nonce % 4096 == 0 and time.perf_counter() - started > self.config.pow_timeout:
                raise CaptchaRuntimeError("proof-of-work timed out")

    def extract_sid(self, *, app_data: str = "", sess: str = "") -> str:
        if app_data and re.fullmatch(r"\d{10,}", str(app_data)):
            return str(app_data)
        if sess:
            token = str(sess).split("_", 1)[0]
            if re.fullmatch(r"\d{10,}", token):
                return token
        return ""

    def report_cap_monitor(
        self,
        *,
        aid: int,
        sess: str,
        app_data: str = "",
        speed_list: Sequence[Mapping[str, Any]] | None = None,
        is_preload: bool = True,
        is_visible: bool = True,
    ) -> None:
        payload = {
            "is_visible": 1 if is_visible else 0,
            "is_preload": 1 if is_preload else 0,
            "speed_list": [
                {
                    "name": str(item.get("name", "")),
                    "duration": int(max(0, round(float(item.get("duration", 0))))),
                }
                for item in (speed_list or [])
                if item.get("name")
            ],
        }
        params = {
            "appid": aid,
            "sid": self.extract_sid(app_data=app_data, sess=sess),
            "log_mode": "monitor",
            "client": "Chrome",
            "platform": "Windows",
            "data": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        try:
            self.session.get(
                CAP_MONITOR_URL,
                params=params,
                headers=self.fetch_headers(
                    referer=CAPTCHA_REFERER,
                    accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    mode="no-cors",
                    destination="image",
                ),
                timeout=self.timeout,
            )
        except Exception:
            pass

    def verify(
        self,
        *,
        sess: str,
        collect: str,
        eks: str,
        answers: Sequence[Sequence[int]],
        pow_answer: str,
        pow_time_ms: int,
    ) -> dict[str, Any]:
        answer_json = json.dumps(
            [
                {
                    "elem_id": index + 1,
                    "type": "DynAnswerType_POS",
                    "data": f"{point[0]},{point[1]}",
                }
                for index, point in enumerate(answers)
            ],
            separators=(",", ":"),
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.post(
                    VERIFY_URL,
                    data={
                        "collect": collect,
                        "tlg": len(collect),
                        "eks": eks,
                        "sess": sess,
                        "ans": answer_json,
                        "pow_answer": pow_answer,
                        "pow_calc_time": pow_time_ms,
                    },
                    timeout=self.timeout,
                    headers={
                        **self.fetch_headers(referer=CAPTCHA_REFERER),
                        "Origin": "https://turing.captcha.gtimg.com",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                status_code = int(getattr(response, "status_code", 200))
                if status_code == 429 or status_code >= 500:
                    raise CaptchaRuntimeError(f"HTTP {status_code}")
                response.raise_for_status()
                result = response.json()
                break
            except REQUEST_ERRORS as error:
                if not self._can_retry_http(attempt):
                    raise CaptchaRuntimeError(f"verification request failed: {error}") from error
                time.sleep(self.config.http_retry_interval)
            except CaptchaRuntimeError as error:
                if not self._can_retry_http(attempt):
                    raise CaptchaRuntimeError(f"verification request failed: {error}") from error
                time.sleep(self.config.http_retry_interval)
            except ValueError as error:
                raise CaptchaRuntimeError("verification response is not JSON") from error
        if not isinstance(result, dict):
            raise CaptchaRuntimeError("verification response must be an object")
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.tdc.close()
        if self._owns_session:
            self.session.close()
