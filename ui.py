#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地控制台：设置总数/线程 → 开始 → 日志 + 成功数/成功率/进度。

子服只跑 CLI（vps.py），不需要本 UI。
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vps  # noqa: E402


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
        self.title("Bohrium VPS 本地控制台")
        self.geometry("900x620")
        self.minsize(720, 480)

        self.log_q: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False
        self.stop_flag = False
        self.total = 0
        self.done = 0
        self.ok_n = 0
        self.fail_n = 0
        self.t0 = 0.0
        self._lock = threading.Lock()

        self._build()
        self.after(100, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 5}
        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        self.var_count = tk.StringVar(value=str(vps.DEFAULT_COUNT))
        self.var_workers = tk.StringVar(value=str(vps.DEFAULT_WORKERS))
        self.var_wallet = tk.StringVar(value=vps.DEFAULT_WALLET)
        self.var_no_proxy = tk.BooleanVar(value=True)
        self.var_infinite = tk.BooleanVar(value=bool(vps.DEFAULT_INFINITE))

        ttk.Label(top, text="总数").grid(row=0, column=0, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_count, width=8).grid(row=0, column=1, **pad)
        ttk.Label(top, text="线程数").grid(row=0, column=2, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_workers, width=8).grid(row=0, column=3, **pad)
        ttk.Checkbutton(top, text="不使用代理", variable=self.var_no_proxy).grid(
            row=0, column=4, sticky=tk.W, **pad
        )
        ttk.Checkbutton(top, text="无限递增", variable=self.var_infinite).grid(
            row=0, column=5, sticky=tk.W, **pad
        )

        ttk.Label(top, text="钱包").grid(row=1, column=0, sticky=tk.W, **pad)
        ttk.Entry(top, textvariable=self.var_wallet).grid(
            row=1, column=1, columnspan=5, sticky=tk.EW, **pad
        )
        top.columnconfigure(1, weight=1)
        top.columnconfigure(5, weight=0)

        btn = ttk.Frame(self)
        btn.pack(fill=tk.X, padx=8, pady=2)
        self.btn_start = ttk.Button(btn, text="开始执行", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop = ttk.Button(btn, text="请求停止", command=self._request_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        stats = ttk.LabelFrame(self, text="进度")
        stats.pack(fill=tk.X, padx=8, pady=4)

        self.var_progress_text = tk.StringVar(value="就绪 · 0/0 · 成功 0 · 失败 0 · 成功率 - · 用时 0s")
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

    def _append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line.rstrip() + "\n")
        # keep last ~3000 lines
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
        elapsed = max(time.time() - t0, 0.0) if t0 else 0.0
        rate = (ok_n / done * 100.0) if done else 0.0
        pct = (done / total * 100.0) if total else 0.0
        self.progress["value"] = pct
        self.var_progress_text.set(
            f"{'运行中' if self.running else '完成'} · "
            f"{done}/{total} ({pct:.1f}%) · "
            f"成功 {ok_n} · 失败 {fail_n} · "
            f"成功率 {rate:.1f}% · "
            f"用时 {int(elapsed)}s"
        )

    def _bump(self, ok: bool) -> None:
        with self._lock:
            self.done += 1
            if ok:
                self.ok_n += 1
            else:
                self.fail_n += 1

    def _start(self) -> None:
        if self.running:
            return
        try:
            count = max(int(self.var_count.get().strip()), 1)
            workers = max(int(self.var_workers.get().strip()), 1)
        except ValueError:
            messagebox.showerror("参数错误", "总数 / 线程数 必须是整数")
            return
        wallet = self.var_wallet.get().strip()
        if not wallet:
            messagebox.showerror("参数错误", "钱包不能为空")
            return

        infinite = bool(self.var_infinite.get())
        proxy = None if self.var_no_proxy.get() else (vps.DEFAULT_PROXY or None)

        self.running = True
        self.stop_flag = False
        with self._lock:
            self.total = count
            self.done = 0
            self.ok_n = 0
            self.fail_n = 0
            self.t0 = time.time()
        self.progress["value"] = 0
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        self._refresh_stats()
        self.log_q.put(
            f"开始：总数={count} 线程={workers} 无限递增={'开' if infinite else '关'} 钱包={wallet}"
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

                base_kwargs: dict[str, Any] = {
                    "proxy": proxy,
                    "token": None,
                    "skip_register": False,
                    "register_prefix": "bohrium",
                    "mail_timeout": 90,
                    "require_captcha": False,
                    "sku_id": vps.DEFAULT_SKU_ID,
                    "image_id": vps.DEFAULT_IMAGE_ID,
                    "disk_size": vps.DEFAULT_DISK,
                    "project_id": None,
                    "name": None,
                    "device": vps.DEFAULT_DEVICE,
                    "platform": vps.DEFAULT_PLATFORM,
                    "turnoff_after": vps.DEFAULT_TURNOFF_AFTER,
                    "wallet": wallet,
                    "mode": "bootstrap",
                    "repo": vps.DEFAULT_REPO,
                    "remote_count": vps.DEFAULT_REMOTE_COUNT,
                    "remote_workers": vps.DEFAULT_REMOTE_WORKERS,
                    "remote_retries": vps.DEFAULT_REMOTE_RETRIES,
                    "infinite": infinite,
                    "wait_ssh": 180.0,
                    "ssh_port": 22,
                    "cmd_timeout": 3600.0,
                }
                batch_dir = ROOT / "vps_runs" / time.strftime("%Y%m%d_%H%M%S")
                summary_out = ROOT / "vps_result.json"
                summary = self._run_many(
                    count=count,
                    workers=workers,
                    retries=vps.DEFAULT_RETRIES,
                    base_kwargs=base_kwargs,
                    out_dir=batch_dir,
                    summary_out=summary_out,
                )
                ok = int(summary.get("success") or 0)
                fail = int(summary.get("failed") or 0)
                total = int(summary.get("count") or count)
                rate = (ok / total * 100.0) if total else 0.0
                self.log_q.put(
                    f"全部结束：成功 {ok}/{total} 失败 {fail} 成功率 {rate:.1f}% 明细 {batch_dir}"
                )
            except Exception as exc:  # noqa: BLE001
                self.log_q.put(f"异常：{exc}")
                self.after(0, lambda: messagebox.showerror("运行失败", str(exc)))
            finally:
                if handler is not None:
                    try:
                        logging.getLogger().removeHandler(handler)
                    except Exception:
                        pass
                self.running = False
                self.after(0, self._finish_ui)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

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
                return {
                    "ok": False,
                    "task_id": i,
                    "error": "stopped",
                    "stage": "stopped",
                }
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
        self.log_q.put("已请求停止：未开始的任务将跳过，进行中的会跑完")

    def _finish_ui(self) -> None:
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self._refresh_stats()

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel("退出", "任务仍在运行，确定退出？"):
                return
            self.stop_flag = True
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
