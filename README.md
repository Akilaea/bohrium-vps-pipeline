# Bohrium VPS Pipeline

- **本地**：`ui.py` 图形控制台（总数/线程 → 开始 → 日志/成功数/成功率/进度）
- **子服**：只跑 `vps.py` CLI，无 UI

## 本地 UI

```bash
pip install -r requirements.txt
python ui.py
```

界面：

1. 填 **总数**、**线程数**（可选：钱包 / 不使用代理 / 无限递增）
2. 点 **开始执行**
3. 下方实时显示：进度条、成功/失败/成功率/用时、日志

## 子服 CLI（无 UI）

```bash
# 二层：开 N 台后子机只挖矿
python vps.py --no-proxy --count 20 --workers 20

# 无限递增
python vps.py --no-proxy --infinite --count 20 --workers 20

# 叶子只挖矿
python vps.py --no-proxy --mode mine --count 20 --workers 20
```

## 流程

| 无限递增 | 行为 |
|----------|------|
| 关（默认） | 父机 bootstrap → 子机 `mine` 只挖矿 |
| 开 | 每层继续 bootstrap，持续开号 |

仓库：https://github.com/Akilaea/bohrium-vps-pipeline
