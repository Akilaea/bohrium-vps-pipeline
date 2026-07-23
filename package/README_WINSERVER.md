# Windows Server 独立部署包

目标：Windows Server 2019 / 2022 / **2025**，**无需安装 Python**。

## 在本机构建

```powershell
cd F:\opencode\adminner
powershell -ExecutionPolicy Bypass -File .\build_win.ps1
```

产物目录：`dist\BohriumVPS\`

将整个 **`BohriumVPS` 文件夹** 拷到服务器即可。

## 服务器上使用

1. 双击 `BohriumVPS.exe`
2. 设置 **总数 / 线程数**
3. 选择 **有限递增** 或 **无限递增**
4. （可选）启用 **定时**，间隔例如 `30` 分钟
5. 点 **开始执行** 或 **启动定时**

## 功能对照

| 功能 | 说明 |
|------|------|
| 有限递增 | 本机开 N 台；子机 `mine` 只挖矿 |
| 无限递增 | 每层继续 `bootstrap --infinite` |
| 机型回退 | 高配→低配依次尝试 |
| 定时跑 | 间隔 N 分钟自动再跑一轮 |
| 进度 | 成功数 / 失败 / 成功率 / 进度条 / 日志 |

## 同目录产生的文件

- `ui_config.json` — 界面参数
- `vps_result.json` — 最近一轮汇总
- `vps_runs\` — 明细

## 建议

- 服务器需能访问 `platform.bohrium.com` / `www.bohrium.com`
- 防火墙放行出站 HTTPS / SSH(22)
- 杀软对 `BohriumVPS.exe` 加白名单
