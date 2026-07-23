# Bohrium VPS Pipeline（本地版）

注册账号 → 创建节点 → SSH → 按模式执行。

> 仅支持本地/国内网络环境。海外 IP 通常无法创建节点。

## 流程（默认 bootstrap）

1. **本机** 并发注册并创建 N 台 VPS（默认 20 线程 / 20 号）
2. 每台 VPS 上：
   - `git clone` 本仓库
   - 本机安装挖矿
   - 再跑 `python vps.py --mode mine --count 20 --workers 20`：再注册 20 号开机器
3. **二阶机器** 只挖矿（`--mode mine`），不再拉仓库开号，防止无限递归

```
本机(bootstrap)
  └─ VPS-1..N  clone + 自己挖矿 + 再开 20 台(mine)
        └─ 叶子 VPS  只挖矿
```

## 安装

```bash
pip install -r requirements.txt
```

## UI 控制台

```bash
python ui.py
```

| 项 | 默认 |
|----|------|
| 任务数 / 线程 | 20 / 20 |
| 模式 | `bootstrap` |
| 远程开号 / 线程 | 20 / 20 |
| 仓库 | `https://github.com/Akilaea/bohrium-vps-pipeline.git` |
| 钱包 | `TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat` |

## 命令行

```bash
# 默认：本机开 20 台，每台再拉仓库开 20 台叶子并挖矿
python vps.py --no-proxy

# 仅挖矿（叶子）
python vps.py --no-proxy --mode mine --count 20 --workers 20

# 自定义
python vps.py --no-proxy --mode bootstrap \
  --count 5 --workers 5 \
  --remote-count 20 --remote-workers 20 \
  --repo https://github.com/Akilaea/bohrium-vps-pipeline.git
```

## 模块

- `ui.py` — 本地控制台
- `vps.py` — 编排（bootstrap / mine）
- `bohrium_register.py` / `bohrium_create_node.py` / `bohrium_ssh.py`
