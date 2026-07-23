# Bohrium VPS Pipeline（本地版）

注册账号 → 创建节点 → SSH → 按模式执行。

> 仅支持本地/国内网络环境。海外 IP 通常无法创建节点。

## 流程（默认 bootstrap）

1. **本机** 并发注册并创建 N 台 VPS（默认 20 线程 / 20 号）
2. 每台 VPS 上：
   - `git clone` 本仓库
   - 本机安装挖矿
   - 再开下一层机器（由「无限递增」开关决定模式）

### 无限递增（可选）

| 开关 | 行为 |
|------|------|
| **关闭（默认）** | 子机 `--mode mine`，只挖矿，共 2 层 |
| **开启** | 子机继续 `--mode bootstrap --infinite`，每层再开 N 台，无限递增 |

```
关闭无限递增:
本机(bootstrap)
  └─ 父 VPS  clone + 挖矿 + 开 20 台(mine)
        └─ 叶子  只挖矿

开启无限递增:
本机(bootstrap --infinite)
  └─ 每层 VPS  clone + 挖矿 + 再开 20 台(bootstrap --infinite)
        └─ ... 持续递增
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
| 无限递增 | 关闭 |
| 远程开号 / 线程 | 20 / 20 |
| 仓库 | `https://github.com/Akilaea/bohrium-vps-pipeline.git` |
| 钱包 | `TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat` |

## 命令行

```bash
# 默认：本机开 20 台，每台再开 20 叶子（仅挖矿，不无限递增）
python vps.py --no-proxy

# 开启无限递增：每层子机继续 bootstrap 开号
python vps.py --no-proxy --infinite

# 仅挖矿（叶子）
python vps.py --no-proxy --mode mine --count 20 --workers 20

# 自定义
python vps.py --no-proxy --mode bootstrap --infinite \
  --count 5 --workers 5 \
  --remote-count 20 --remote-workers 20 \
  --repo https://github.com/Akilaea/bohrium-vps-pipeline.git
```

## 模块

- `ui.py` — 本地控制台
- `vps.py` — 编排（bootstrap / mine）
- `bohrium_register.py` / `bohrium_create_node.py` / `bohrium_ssh.py`
