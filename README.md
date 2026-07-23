# Bohrium VPS Pipeline（本地版）

注册账号 → 创建节点 → SSH → C3Pool 挖矿。

> 仅支持本地运行（国内/可用代理环境）。海外 IP / GitHub Actions 无法创建节点。

## 安装

```bash
pip install -r requirements.txt
```

## 最小化 UI 控制台（推荐）

```bash
python ui.py
```

默认参数：

| 项 | 默认 |
|----|------|
| 任务数 | 20 |
| 线程数 | 20 |
| 重试 | 2 |
| 钱包 | `TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat` |
| 代理 | `http://127.0.0.1:7890`（可勾选不使用代理） |

界面可改参数、查看任务表格与实时日志。点 **开始** 即按 20 线程并发注册并开挖。

## 命令行

```bash
# 默认 20 任务 / 20 线程 / 2 重试
python vps.py --no-proxy

python vps.py --count 20 --workers 20 --retries 2 --no-proxy
python vps.py --wallet TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat
```

## 模块

- `ui.py` — 本地最小化控制台
- `vps.py` — 流水线编排
- `bohrium_register.py` — 注册登录
- `bohrium_create_node.py` — 创建节点
- `bohrium_ssh.py` — SSH 执行

结果写入 `vps_result.json` 与 `vps_runs/`。
