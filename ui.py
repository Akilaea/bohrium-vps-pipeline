#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地控制台（可打包独立运行）：总数/线程、有限/无限递增、定时自动跑、进度与日志。

子服只跑 vps.py CLI，不需要本 UI。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

from paths import app_dir

ROOT = app_dir()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_res = Path(getattr(sys, "_MEIPASS", str(ROOT)))  # type: ignore[attr-defined]
if str(_res) not in sys.path:
    sys.path.insert(0, str(_res))

import vps  # noqa: E402

CONFIG_PATH = ROOT / "ui_config.json"


class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue[str]) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bohrium VPS 控制台 · Win 独立版")
        self.geometry("960x680")
        self.minsize(800, 520)

        self.log_q: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self.running = False
        self._active_rounds = 0
        self.stop_flag = False
        self.total = 0
        self.done = 0
        self.ok_n = 0
        self.fail_n = 0
        self.t0 = 0.0
        self._lock = threading.Lock()
        self._timer_job: str | None = None
        self._next_run_at: float | None = None
        self._run_round = 0

        self._build()
        self._load_config()
        self.after(100, self._drain_logs)
        self.after(500, self._tick_timer_label)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 4}
        top = ttk.LabelFrame(self, text="任务参数")
        top.pack(fill=tk.X, padx=8, pady=4)

        self.var_count = tk.StringVar(value=str(vps.DEFAULT_COUNT))
        self.var_workers = tk.StringVar(value=str(vps.DEFAULT_WORKERS))
        # 独立地址：留空则走默认 OWNER_WALLET；填写则覆盖并向下传递
        self.var_wallet = tk.StringVar(value="")
        self.var_no_proxy = tk.BooleanVar(value=True)
        self.var_expand = tk.StringVar(value="有限递增")  # 有限递增 | 无限递增
        self.var_product = tk.StringVar(value="Start Nodes")  # Start Nodes | Run Notebooks
        self.var_sku_mode = tk.StringVar(value="固定388")  # 固定388 | 递减回退
        self.var_sku_id = tk.StringVar(value=str(vps.DEFAULT_SKU_ID_FIXED))
        self.var_remote_count = tk.StringVar(value=str(vps.DEFAULT_REMOTE_COUNT))
        self.var_remote_workers = tk.StringVar(value=str(vps.DEFAULT_REMOTE_WORKERS))
        self.var_schedule = tk.BooleanVar(value=False)
        self.var_interval_min = tk.StringVar(value="30")
        self.var_run_on_start = tk.BooleanVar(value=True)

        r = 0
        ttk.Label(top, text="总数").grid(row=r, column=0, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_count, width=8).grid(row=r, column=1, **pad)
        ttk.Label(top, text="线程数").grid(row=r, column=2, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_workers, width=8).grid(row=r, column=3, **pad)
        ttk.Checkbutton(top, text="不使用代理", variable=self.var_no_proxy).grid(
            row=r, column=4, sticky=tk.W, **pad
        )

        r = 1
        ttk.Label(top, text="递增模式").grid(row=r, column=0, sticky=tk.W, **pad)
        ttk.Combobox(
            top,
            textvariable=self.var_expand,
            values=["有限递增", "无限递增"],
            width=12,
            state="readonly",
        ).grid(row=r, column=1, sticky=tk.W, **pad)
        ttk.Label(top, text="产品模式").grid(row=r, column=2, sticky=tk.W, **pad)
        ttk.Combobox(
            top,
            textvariable=self.var_product,
            values=["Start Nodes", "Run Notebooks"],
            width=14,
            state="readonly",
        ).grid(row=r, column=3, sticky=tk.W, **pad)
        ttk.Label(top, text="远程开号").grid(row=r, column=4, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_remote_count, width=6).grid(row=r, column=5, **pad)

        r = 2
        ttk.Label(top, text="远程线程").grid(row=r, column=0, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_remote_workers, width=8).grid(row=r, column=1, **pad)
        ttk.Label(top, text="SKU模式").grid(row=r, column=2, sticky=tk.W, **pad)
        ttk.Combobox(
            top,
            textvariable=self.var_sku_mode,
            values=["固定388", "递减回退"],
            width=12,
            state="readonly",
        ).grid(row=r, column=3, sticky=tk.W, **pad)
        ttk.Label(top, text="skuId").grid(row=r, column=4, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_sku_id, width=8).grid(row=r, column=5, **pad)

        r = 3
        ttk.Label(top, text="独立钱包").grid(row=r, column=0, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_wallet).grid(
            row=r, column=1, columnspan=5, sticky=tk.EW, **pad
        )
        r = 4
        ttk.Label(
            top,
            text=f"SKU 固定=只开指定机型(默认388)；递减=高→低回退；钱包留空= {vps.OWNER_WALLET}",
            foreground="#666",
        ).grid(row=r, column=1, columnspan=5, sticky=tk.W, padx=6, pady=(0, 4))
        top.columnconfigure(3, weight=1)

        sched = ttk.LabelFrame(self, text="定时自动跑")
        sched.pack(fill=tk.X, padx=8, pady=4)
        ttk.Checkbutton(sched, text="启用定时", variable=self.var_schedule).grid(
            row=0, column=0, sticky=tk.W, **pad
        )
        ttk.Label(sched, text="间隔(分钟)").grid(row=0, column=1, sticky=tk.W, **pad)
        ttk.Entry(sched, textvariable=self.var_interval_min, width=8).grid(row=0, column=2, **pad)
        ttk.Checkbutton(sched, text="启动后立即跑一轮", variable=self.var_run_on_start).grid(
            row=0, column=3, sticky=tk.W, **pad
        )
        self.var_timer_text = tk.StringVar(value="定时：未启用")
        ttk.Label(sched, textvariable=self.var_timer_text).grid(
            row=0, column=4, sticky=tk.W, padx=12
        )

        btn = ttk.Frame(self)
        btn.pack(fill=tk.X, padx=8, pady=2)
        self.btn_start = ttk.Button(btn, text="开始执行", command=self._start_once)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop = ttk.Button(btn, text="请求停止", command=self._request_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        self.btn_timer = ttk.Button(btn, text="启动定时", command=self._toggle_schedule)
        self.btn_timer.pack(side=tk.LEFT, padx=4)
        ttk.Button(btn, text="保存配置", command=self._save_config).pack(side=tk.LEFT, padx=4)

        stats = ttk.LabelFrame(self, text="进度")
        stats.pack(fill=tk.X, padx=8, pady=4)
        self.var_progress_text = tk.StringVar(
            value="就绪 · 0/0 · 成功 0 · 失败 0 · 成功率 - · 用时 0s"
        )
        ttk.Label(stats, textvariable=self.var_progress_text, font=("", 10)).pack(
            anchor=tk.W, padx=8, pady=(6, 2)
        )
        self.progress = ttk.Progressbar(stats, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill=tk.X, padx=8, pady=(2, 8))

        log_fr = ttk.LabelFrame(self, text="日志")
        log_fr.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.log = scrolledtext.ScrolledText(
            log_fr, height=18, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        tip = (
            "产品：Start Nodes 容器（默认）/ Run Notebooks（第二模式） | "
            "独立钱包留空=默认地址 | 有限递增=子机只挖矿 | 无限递增=每层继续开号 | "
            "定时：间隔到点自动再跑（上一轮未结束则跳过）"
        )
        ttk.Label(self, text=tip, foreground="#555").pack(anchor=tk.W, padx=10, pady=(0, 6))

    # ----- config -----
    def _load_config(self) -> None:
        if not CONFIG_PATH.is_file():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        mapping = {
            "count": self.var_count,
            "workers": self.var_workers,
            "wallet": self.var_wallet,
            "remote_count": self.var_remote_count,
            "remote_workers": self.var_remote_workers,
            "interval_min": self.var_interval_min,
            "expand": self.var_expand,
            "product": self.var_product,
            "sku_mode": self.var_sku_mode,
            "sku_id": self.var_sku_id,
        }
        for k, var in mapping.items():
            if k in data and data[k] is not None:
                val = str(data[k])
                if k == "product":
                    if val in ("notebook", "Run Notebooks", "Notebook", "notebooks"):
                        val = "Run Notebooks"
                    else:
                        val = "Start Nodes"
                if k == "sku_mode":
                    if val in ("desc", "递减", "递减回退", "fallback", "auto"):
                        val = "递减回退"
                    else:
                        val = "固定388"
                # legacy device field ignored
                var.set(val)
        if "no_proxy" in data:
            self.var_no_proxy.set(bool(data["no_proxy"]))
        if "schedule" in data:
            self.var_schedule.set(bool(data["schedule"]))
        if "run_on_start" in data:
            self.var_run_on_start.set(bool(data["run_on_start"]))
        if data.get("schedule"):
            self.after(800, self._start_schedule)

    def _save_config(self) -> None:
        data = {
            "count": self.var_count.get(),
            "workers": self.var_workers.get(),
            "wallet": self.var_wallet.get(),
            "no_proxy": bool(self.var_no_proxy.get()),
            "expand": self.var_expand.get(),
            "product": self.var_product.get(),
            "sku_mode": self.var_sku_mode.get(),
            "sku_id": self.var_sku_id.get(),
            "remote_count": self.var_remote_count.get(),
            "remote_workers": self.var_remote_workers.get(),
            "schedule": bool(self.var_schedule.get()),
            "interval_min": self.var_interval_min.get(),
            "run_on_start": bool(self.var_run_on_start.get()),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log_q.put(f"配置已保存：{CONFIG_PATH}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    # ----- logging / stats -----
    def _append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line.rstrip() + "\n")
        try:
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > 3200:
                self.log.delete("1.0", f"{lines - 3000}.0")
        except Exception:
            pass
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _drain_logs(self) -> None:
        try:
            while True:
                self._append_log(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if self.running:
            self._refresh_stats()
        self.after(100, self._drain_logs)

    def _refresh_stats(self) -> None:
        with self._lock:
            total = self.total
            done = self.done
            ok_n = self.ok_n
            fail_n = self.fail_n
            t0 = self.t0
            rnd = self._run_round
        elapsed = max(time.time() - t0, 0.0) if t0 else 0.0
        rate = (ok_n / done * 100.0) if done else 0.0
        pct = (done / total * 100.0) if total else 0.0
        self.progress["value"] = pct
        with self._lock:
            active = self._active_rounds
        if active > 0:
            state = f"运行中(并行{active}轮)"
        else:
            state = "就绪"
        self.var_progress_text.set(
            f"{state} · 累计第{rnd}轮 · {done}/{total} ({pct:.1f}%) · "
            f"成功 {ok_n} · 失败 {fail_n} · 成功率 {rate:.1f}% · 用时 {int(elapsed)}s"
        )

    def _bump(self, ok: bool) -> None:
        with self._lock:
            self.done += 1
            if ok:
                self.ok_n += 1
            else:
                self.fail_n += 1

    # ----- schedule -----
    def _toggle_schedule(self) -> None:
        if self._timer_job is not None or self.var_schedule.get():
            if self._timer_job is not None and not self.var_schedule.get():
                self._stop_schedule()
                return
        if self.var_schedule.get() and self._timer_job is not None:
            self._stop_schedule()
            self.var_schedule.set(False)
            self.btn_timer.configure(text="启动定时")
            self.log_q.put("定时已停止")
            return
        self.var_schedule.set(True)
        self._start_schedule()

    def _start_schedule(self) -> None:
        try:
            mins = max(float(self.var_interval_min.get().strip()), 1.0)
        except ValueError:
            messagebox.showerror("参数错误", "间隔分钟必须是数字")
            self.var_schedule.set(False)
            return
        self.var_schedule.set(True)
        self.btn_timer.configure(text="停止定时")
        self.log_q.put(f"定时已启用：每 {mins:g} 分钟跑一轮")
        if self.var_run_on_start.get() and not self.running:
            self._start_once(from_timer=True)
        self._arm_timer(mins * 60.0)

    def _stop_schedule(self) -> None:
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        self._next_run_at = None
        self.btn_timer.configure(text="启动定时")
        self.var_timer_text.set("定时：未启用")

    def _arm_timer(self, delay_sec: float) -> None:
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
        self._next_run_at = time.time() + max(delay_sec, 5.0)
        ms = int(max(delay_sec, 5.0) * 1000)
        self._timer_job = self.after(ms, self._on_timer_fire)

    def _on_timer_fire(self) -> None:
        self._timer_job = None
        if not self.var_schedule.get():
            self.var_timer_text.set("定时：未启用")
            return
        # 上一轮未结束也并行开新一轮（节点长等待不再阻塞定时）
        if self.running:
            self.log_q.put(
                f"定时触发：上一轮仍在运行(并行轮次={self._active_rounds})，同时开启新一轮"
            )
        self._start_once(from_timer=True)
        try:
            mins = max(float(self.var_interval_min.get().strip()), 1.0)
        except ValueError:
            mins = 30.0
        self._arm_timer(mins * 60.0)

    def _tick_timer_label(self) -> None:
        if self.var_schedule.get() and self._next_run_at:
            left = max(int(self._next_run_at - time.time()), 0)
            m, s = divmod(left, 60)
            self.var_timer_text.set(f"定时：已启用 · 下次约 {m}分{s:02d}秒后")
        elif not self.var_schedule.get():
            self.var_timer_text.set("定时：未启用")
        self.after(500, self._tick_timer_label)

    # ----- run -----
    def _parse_params(self) -> dict[str, Any] | None:
        try:
            count = max(int(self.var_count.get().strip()), 1)
            workers = max(int(self.var_workers.get().strip()), 1)
            remote_count = max(int(self.var_remote_count.get().strip()), 1)
            remote_workers = max(int(self.var_remote_workers.get().strip()), 1)
        except ValueError:
            messagebox.showerror("参数错误", "总数/线程/远程参数必须是整数")
            return None
        # 独立地址可选：有填写则传递该地址，否则 resolve 为默认 OWNER_WALLET
        wallet = vps.resolve_wallet(self.var_wallet.get())
        infinite = self.var_expand.get().strip() == "无限递增"
        product = (
            "notebook"
            if self.var_product.get().strip() in {"Run Notebooks", "notebook", "Notebook"}
            else "node"
        )
        proxy = None if self.var_no_proxy.get() else (vps.DEFAULT_PROXY or None)
        return {
            "count": count,
            "workers": workers,
            "remote_count": remote_count,
            "remote_workers": remote_workers,
            "wallet": wallet,
            "wallet_custom": bool(self.var_wallet.get().strip()),
            "infinite": infinite,
            "product": product,
            "proxy": proxy,
        }

    def _start_once(self, from_timer: bool = False) -> None:
        # 允许多轮并行：定时/手动在上一轮未结束时仍可开新一轮
        params = self._parse_params()
        if not params:
            return
        self._save_config()

        count = params["count"]
        workers = params["workers"]
        infinite = params["infinite"]
        wallet = params["wallet"]
        proxy = params["proxy"]
        product = params["product"]
        remote_count = params["remote_count"]
        remote_workers = params["remote_workers"]

        with self._lock:
            if self._active_rounds <= 0:
                self.total = count
                self.done = 0
                self.ok_n = 0
                self.fail_n = 0
                self.t0 = time.time()
                self.progress["value"] = 0
            else:
                # 并行新一轮：进度分母累加，不清空已完成计数
                self.total += count
            self._active_rounds += 1
            self._run_round += 1
            rnd = self._run_round
            active = self._active_rounds
        self.running = True
        # 并行轮次各自 stop_flag：仅手动「请求停止」影响后续新任务
        if not from_timer and active == 1:
            self.stop_flag = False
        self.btn_start.configure(state=tk.NORMAL)  # 允许再点开并行轮
        self.btn_stop.configure(state=tk.NORMAL)
        if not from_timer and active == 1:
            self.log.configure(state=tk.NORMAL)
            self.log.delete("1.0", tk.END)
            self.log.configure(state=tk.DISABLED)
        self._refresh_stats()
        mode_txt = "无限递增" if infinite else "有限递增"
        product_txt = "Run Notebooks" if product == "notebook" else "Start Nodes"
        wallet_src = "自定义" if params.get("wallet_custom") else "默认"
        sku_mode_txt = self.var_sku_mode.get()
        self.log_q.put(
            f"===== 第{rnd}轮开始 ===== 并行中={active} 总数={count} 线程={workers} "
            f"模式={mode_txt} 产品={product_txt} SKU={sku_mode_txt}/{self.var_sku_id.get()} "
            f"远程={remote_count}x{remote_workers} "
            f"钱包({wallet_src})={wallet}"
        )

        def run() -> None:
            handler: logging.Handler | None = None
            try:
                vps.setup_logging(verbose=False)
                root = logging.getLogger()
                handler = _QueueHandler(self.log_q)
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
                )
                root.addHandler(handler)

                sku_mode = "desc" if "递减" in (self.var_sku_mode.get() or "") else "fixed"
                try:
                    sku_id_val = int((self.var_sku_id.get() or "").strip() or vps.DEFAULT_SKU_ID_FIXED)
                except ValueError:
                    sku_id_val = vps.DEFAULT_SKU_ID_FIXED
                base_kwargs: dict[str, Any] = {
                    "proxy": proxy,
                    "token": None,
                    "skip_register": False,
                    "register_prefix": "bohrium",
                    "mail_timeout": 90,
                    "require_captcha": False,
                    "sku_id": sku_id_val if sku_mode == "fixed" else (sku_id_val or None),
                    "sku_mode": sku_mode,
                    "image_id": vps.DEFAULT_IMAGE_ID,
                    "disk_size": vps.DEFAULT_DISK,
                    "project_id": None,
                    "name": None,
                    "device": "container",
                    "product": product,
                    "platform": vps.DEFAULT_PLATFORM,
                    "turnoff_after": vps.DEFAULT_TURNOFF_AFTER,
                    "wallet": wallet,
                    "mode": "bootstrap",
                    "repo": vps.DEFAULT_REPO,
                    "remote_count": remote_count,
                    "remote_workers": remote_workers,
                    "remote_retries": vps.DEFAULT_REMOTE_RETRIES,
                    "infinite": infinite,
                    "wait_ssh": 180.0,
                    "ssh_port": 22,
                    "cmd_timeout": 3600.0,
                }
                stamp = time.strftime("%Y%m%d_%H%M%S")
                batch_dir = ROOT / "vps_runs" / f"{stamp}_r{rnd:03d}"
                summary_out = batch_dir / "summary.json"
                summary = self._run_many(
                    count=count,
                    workers=workers,
                    retries=vps.DEFAULT_RETRIES,
                    base_kwargs=base_kwargs,
                    out_dir=batch_dir,
                    summary_out=summary_out,
                )
                # 最新一轮也写到根目录，便于查看
                try:
                    vps._save(ROOT / "vps_result.json", summary)
                except Exception:
                    pass
                ok = int(summary.get("success") or 0)
                fail = int(summary.get("failed") or 0)
                total = int(summary.get("count") or count)
                rate = (ok / total * 100.0) if total else 0.0
                self.log_q.put(
                    f"===== 第{rnd}轮结束 ===== 成功 {ok}/{total} 失败 {fail} "
                    f"成功率 {rate:.1f}% 明细 {batch_dir}"
                )
            except Exception as exc:  # noqa: BLE001
                self.log_q.put(f"第{rnd}轮异常：{exc}")
                if not from_timer:
                    self.after(0, lambda e=exc: messagebox.showerror("运行失败", str(e)))
            finally:
                if handler is not None:
                    try:
                        logging.getLogger().removeHandler(handler)
                    except Exception:
                        pass
                with self._lock:
                    self._active_rounds = max(0, self._active_rounds - 1)
                    still = self._active_rounds > 0
                    left = self._active_rounds
                self.running = still
                self.log_q.put(f"第{rnd}轮线程退出 · 剩余并行轮次={left}")
                self.after(0, self._finish_ui if not still else self._refresh_stats)

        t = threading.Thread(target=run, daemon=True, name=f"vps-round-{rnd}")
        self.worker = t
        self._workers = [w for w in self._workers if w.is_alive()]
        self._workers.append(t)
        t.start()

    def _run_many(
        self,
        *,
        count: int,
        workers: int,
        retries: int,
        base_kwargs: dict[str, Any],
        out_dir: Path,
        summary_out: Path,
    ) -> dict[str, Any]:
        count = max(int(count), 1)
        workers = max(min(int(workers), count), 1)
        out_dir.mkdir(parents=True, exist_ok=True)
        stream_ssh = count == 1

        summary: dict[str, Any] = {
            "ok": False,
            "count": count,
            "workers": workers,
            "retries": retries,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "success": 0,
            "failed": 0,
            "tasks": [],
        }

        def _one(i: int) -> dict[str, Any]:
            if self.stop_flag:
                return {"ok": False, "task_id": i, "error": "stopped", "stage": "stopped"}
            kwargs = dict(base_kwargs)
            kwargs.update(
                {
                    "task_id": i,
                    "retries": retries,
                    "retry_delay": 3.0,
                    "stream_ssh": stream_ssh,
                    "out": out_dir / f"task_{i:03d}.json",
                    "keep_token_file": out_dir / f"task_{i:03d}_register.json",
                    "create_out": out_dir / f"task_{i:03d}_node.json",
                }
            )
            result = vps.run_pipeline_with_retry(**kwargs)
            ok = bool(result.get("ok"))
            reg = result.get("register") or {}
            c = result.get("create") or {}
            self._bump(ok)
            self.log_q.put(
                f"任务{i} {'成功' if ok else '失败'} | "
                f"邮箱={reg.get('email') or '-'} | "
                f"节点={c.get('node_id') or '-'} | "
                f"IP={c.get('ip') or '-'} | "
                f"SKU={c.get('sku_label') or c.get('sku_id') or '-'} | "
                f"错误={vps.short_error(result.get('error') or '-') if not ok else '-'}"
            )
            return result

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, i): i for i in range(1, count + 1)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001
                    r = {
                        "ok": False,
                        "task_id": i,
                        "error": vps.short_error(exc),
                        "stage": "crashed",
                    }
                    self._bump(False)
                    self.log_q.put(f"任务{i} 异常：{r['error']}")
                results.append(r)

        results.sort(key=lambda x: int(x.get("task_id") or 0))
        summary["tasks"] = results
        summary["success"] = sum(1 for r in results if r.get("ok"))
        summary["failed"] = sum(1 for r in results if not r.get("ok"))
        summary["ok"] = summary["failed"] == 0
        summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        vps._save(summary_out, summary)
        return summary

    def _request_stop(self) -> None:
        self.stop_flag = True
        self.log_q.put(
            f"已请求停止：所有并行轮次中未开始的任务将跳过，进行中的会跑完（并行={self._active_rounds}）"
        )

    def _finish_ui(self) -> None:
        self.btn_start.configure(state=tk.NORMAL)
        if self._active_rounds <= 0:
            self.btn_stop.configure(state=tk.DISABLED)
        self._refresh_stats()

    def _on_close(self) -> None:
        if self._active_rounds > 0 or self.running:
            if not messagebox.askokcancel(
                "退出",
                f"仍有 {max(self._active_rounds, 1)} 轮任务在运行，确定退出？",
            ):
                return
            self.stop_flag = True
        self._stop_schedule()
        self._save_config()
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
