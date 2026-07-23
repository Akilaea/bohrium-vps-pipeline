# Bohrium VPS Pipeline

- **本地 UI**：`ui.py` / 独立 `BohriumVPS.exe`
- **子服**：`vps.py` CLI（Linux 上 clone 后跑，无 UI）

## Windows Server 独立包（免装 Python）

```powershell
powershell -ExecutionPolicy Bypass -File .\build_win.ps1
# 产物：dist\BohriumVPS\  整夹拷到 WinServer 2025 双击 BohriumVPS.exe
```

详见 `package/README_WINSERVER.md`。

### UI 能力

| 项 | 说明 |
|----|------|
| 总数 / 线程 | 本机并发开号 |
| 独立钱包 | 留空=默认地址 `TWdsFCGsotzaLMZnyhVyDJ1sHz8hvxqyat`；填写则覆盖并向下传递 |
| 有限递增 | 子机只挖矿 |
| 无限递增 | 子机继续开号 |
| 定时 | 间隔 N 分钟自动跑（如 30） |
| 进度 | 成功数 / 成功率 / 进度条 / 日志 |
| 机型 | 高配→低配自动回退 |

### 兼容已在跑的旧递增节点

- CLI 仍保留 `--wallet` / `--mode` / `--infinite` / `--remote-*`，旧父机拉起的子进程参数可继续解析
- 未传或空 `--wallet` 一律回落到默认地址
- 子机 `git clone` 新版本后行为自动对齐；已在跑进程无需停机

## 源码运行

```bash
pip install -r requirements.txt
python ui.py
```

## 子服 CLI

```bash
python vps.py --no-proxy --count 20 --workers 20
python vps.py --no-proxy --infinite --count 20 --workers 20
python vps.py --no-proxy --mode mine --count 20 --workers 20
```

仓库：https://github.com/Akilaea/bohrium-vps-pipeline

