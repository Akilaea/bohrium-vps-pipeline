#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal local control console for Bohrium VPS pipeline."""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
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
        self.title("Bohrium VPS 控制台")
        self.geometry("920x640")
        self.minsize(780, 520)

        self.log_q: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False
        self.task_rows: dict[int, str] = {}

        self._build()
        self.after(120, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill=tk.X, **pad)

        self.var_count = tk.StringVar(value=str(vps.DEFAULT_COUNT))
        self.var_workers = tk.StringVar(value=str(vps.DEFAULT_WORKERS))
        self.var_retries = tk.StringVar(value=str(vps.DEFAULT_RETRIES))
        self.var_wallet = tk.StringVar(value=vps.DEFAULT_WALLET)
        self.var_proxy = tk.StringVar(value=vps.DEFAULT_PROXY or "")
        self.var_no_proxy = tk.BooleanVar(value=not bool(vps.DEFAULT_PROXY))
        self.var_mode = tk.StringVar(value=vps.DEFAULT_MODE)
        self.var_repo = tk.StringVar(value=vps.DEFAULT_REPO)
        self.var_remote_count = tk.StringVar(value=str(vps.DEFAULT_REMOTE_COUNT))
        self.var_remote_workers = tk.StringVar(value=str(vps.DEFAULT_REMOTE_WORKERS))

        def lab(r: int, c: int, t: str) -> None:
            ttk.Label(frm, text=t).grid(row=r, column=c, sticky=tk.W, **pad)

        lab(0, 0, "任务数")
        ttk.Entry(frm, textvariable=self.var_count, width=8).grid(row=0, column=1, **pad)
        lab(0, 2, "线程数")
        ttk.Entry(frm, textvariable=self.var_workers, width=8).grid(row=0, column=3, **pad)
        lab(0, 4, "重试")
        ttk.Entry(frm, textvariable=self.var_retries, width=8).grid(row=0, column=5, **pad)

        lab(1, 0, "钱包")
        ttk.Entry(frm, textvariable=self.var_wallet, width=52).grid(
            row=1, column=1, columnspan=5, sticky=tk.EW, **pad
        )

        lab(2, 0, "模式")
        ttk.Combobox(
            frm,
            textvariable=self.var_mode,
            values=["bootstrap", "mine"],
            width=12,
            state="readonly",
        ).grid(row=2, column=1, sticky=tk.W, **pad)
        lab(2, 2, "远程开号")
        ttk.Entry(frm, textvariable=self.var_remote_count, width=8).grid(row=2, column=3, **pad)
        lab(2, 4, "远程线程")
        ttk.Entry(frm, textvariable=self.var_remote_workers, width=8).grid(row=2, column=5, **pad)

        lab(3, 0, "仓库")
        ttk.Entry(frm, textvariable=self.var_repo, width=52).grid(
            row=3, column=1, columnspan=5, sticky=tk.EW, **pad
        )

        lab(4, 0, "代理")
        ttk.Entry(frm, textvariable=self.var_proxy, width=36).grid(
            row=4, column=1, columnspan=3, sticky=tk.EW, **pad
        )
        ttk.Checkbutton(frm, text="不使用代理", variable=self.var_no_proxy).grid(
            row=4, column=4, columnspan=2, sticky=tk.W, **pad
        )

        btn = ttk.Frame(self)
        btn.pack(fill=tk.X, **pad)
        self.btn_start = ttk.Button(btn, text="开始", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=6)
        self.btn_stop = ttk.Button(btn, text="标记停止", command=self._request_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=6)
        self.lbl_status = ttk.Label(btn, text="就绪")
        self.lbl_status.pack(side=tk.LEFT, padx=12)

        mid = ttk.Panedwindow(self, orient=tk.VERTICAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        tree_fr = ttk.Frame(mid)
        mid.add(tree_fr, weight=1)
        cols = ("id", "status", "email", "node", "ip", "error")
        self.tree = ttk.Treeview(tree_fr, columns=cols, show="headings", height=10)
        heads = {
            "id": "任务",
            "status": "状态",
            "email": "邮箱",
            "node": "节点",
            "ip": "IP",
            "error": "错误",
        }
        widths = {"id": 50, "status": 80, "email": 220, "node": 90, "ip": 120, "error": 280}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor=tk.W)
        ys = ttk.Scrollbar(tree_fr, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)

        log_fr = ttk.Frame(mid)
        mid.add(log_fr, weight=2)
        self.log = scrolledtext.ScrolledText(log_fr, height=14, wrap=tk.WORD, font=("Consolas", 9))
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.configure(state=tk.DISABLED)

        frm.columnconfigure(1, weight=1)

    def _append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _drain_logs(self) -> None:
        try:
            while True:
                self._append_log(self.log_q.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._drain_logs)

    def _set_task_row(
        self,
        task_id: int,
        *,
        status: str = "",
        email: str = "",
        node: str = "",
        ip: str = "",
        error: str = "",
    ) -> None:
        iid = str(task_id)
        vals = (task_id, status, email, node, ip, error)
        if self.tree.exists(iid):
            cur = list(self.tree.item(iid, "values"))
            for i, v in enumerate(vals):
                if v != "" or i == 0:
                    if i == 0:
                        cur[i] = v
                    elif v != "":
                        cur[i] = v
            self.tree.item(iid, values=tuple(cur))
        else:
            self.tree.insert("", tk.END, iid=iid, values=vals)

    def _start(self) -> None:
        if self.running:
            return
        try:
            count = max(int(self.var_count.get()), 1)
            workers = max(int(self.var_workers.get()), 1)
            retries = max(int(self.var_retries.get()), 0)
            remote_count = max(int(self.var_remote_count.get()), 1)
            remote_workers = max(int(self.var_remote_workers.get()), 1)
        except ValueError:
            messagebox.showerror("参数错误", "任务数 / 线程数 / 重试 / 远程参数 必须是整数")
            return
        wallet = self.var_wallet.get().strip()
        if not wallet:
            messagebox.showerror("参数错误", "钱包地址不能为空")
            return
        mode = (self.var_mode.get() or "bootstrap").strip().lower()
        repo = self.var_repo.get().strip() or vps.DEFAULT_REPO
        proxy = None if self.var_no_proxy.get() else (self.var_proxy.get().strip() or None)

        for item in self.tree.get_children():
            self.tree.delete(item)

        self.running = True
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.lbl_status.configure(text=f"运行中 count={count} workers={workers}")

        def run() -> None:
            try:
                vps.setup_logging(verbose=False)
                root = logging.getLogger()
                handler = _QueueHandler(self.log_q)
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
                root.addHandler(handler)
                for i in range(1, count + 1):
                    self.after(0, lambda i=i: self._set_task_row(i, status="排队"))

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
                    "mode": mode,
                    "repo": repo,
                    "remote_count": remote_count,
                    "remote_workers": remote_workers,
                    "remote_retries": retries,
                    "wait_ssh": 180.0,
                    "ssh_port": 22,
                    "cmd_timeout": 3600.0,
                }

                batch_dir = ROOT / "vps_runs" / time.strftime("%Y%m%d_%H%M%S")
                summary_out = ROOT / "vps_result.json"

                # Hook progress via monkey-patch of log lines is weak; wrap run_many pieces.
                summary = self._run_many_ui(
                    count=count,
                    workers=workers,
                    retries=retries,
                    retry_delay=3.0,
                    base_kwargs=base_kwargs,
                    out_dir=batch_dir,
                    summary_out=summary_out,
                )
                ok = int(summary.get("success") or 0)
                fail = int(summary.get("failed") or 0)
                self.log_q.put(f"完成：成功 {ok} / 失败 {fail}")
                self.after(
                    0,
                    lambda: self.lbl_status.configure(text=f"完成 成功{ok} 失败{fail}"),
                )
            except Exception as exc:  # noqa: BLE001
                self.log_q.put(f"异常：{exc}")
                self.after(0, lambda: messagebox.showerror("运行失败", str(exc)))
            finally:
                self.running = False
                self.after(0, self._reset_buttons)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _run_many_ui(
        self,
        *,
        count: int,
        workers: int,
        retries: int,
        retry_delay: float,
        base_kwargs: dict[str, Any],
        out_dir: Path,
        summary_out: Path,
    ) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

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
            self.after(0, lambda i=i: self._set_task_row(i, status="运行中"))
            kwargs = dict(base_kwargs)
            kwargs.update(
                {
                    "task_id": i,
                    "retries": retries,
                    "retry_delay": retry_delay,
                    "stream_ssh": stream_ssh,
                    "out": out_dir / f"task_{i:03d}.json",
                    "keep_token_file": out_dir / f"task_{i:03d}_register.json",
                    "create_out": out_dir / f"task_{i:03d}_node.json",
                }
            )
            if kwargs.get("name"):
                kwargs["name"] = f"{kwargs['name']}-{i}"
            result = vps.run_pipeline_with_retry(**kwargs)
            reg = result.get("register") or {}
            c = result.get("create") or {}
            status = "成功" if result.get("ok") else "失败"
            self.after(
                0,
                lambda i=i, status=status, reg=reg, c=c, result=result: self._set_task_row(
                    i,
                    status=status,
                    email=str(reg.get("email") or ""),
                    node=str(c.get("node_id") or ""),
                    ip=str(c.get("ip") or ""),
                    error=vps.short_error(result.get("error") or "") if not result.get("ok") else "",
                ),
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
                    r = {"ok": False, "task_id": i, "error": vps.short_error(exc), "stage": "crashed"}
                    self.after(
                        0,
                        lambda i=i, r=r: self._set_task_row(
                            i, status="失败", error=str(r.get("error") or "")
                        ),
                    )
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
        self.log_q.put("停止请求已记录：当前任务会跑完后结束（无法强制中断远程创建）")
        self.lbl_status.configure(text="停止请求已发送（等当前任务结束）")

    def _reset_buttons(self) -> None:
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel("退出", "任务仍在运行，确定退出？"):
                return
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
