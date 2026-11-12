# Windows Wind API 验证：2026-11-12

## 环境配置

- Windows Python 为 3.12.8、64 位，解释器位于 `C:\Users\macon\AppData\Local\Programs\Python\Python312\python.exe`。
- 本仓库独立 `.venv` 已创建，项目依赖已安装；没有复用 `wind API\APP 1` 的虚拟环境。
- 使用官方 SDK：`C:\Users\macon\Wind\Wind.NET.Client\WindNET\x64\WindPy.py`。仓库级 `configure_wind.py --check` 找到该目录，随后仅在本仓库 `.venv\Lib\site-packages\WindPy.pth` 写入 SDK 路径。
- SDK 文件系统元数据没有提供 `FileVersion` / `ProductVersion`；`WindPy.py` 修改时间为 2026-08-29。版本号尚未从官方安装包界面或版本说明独立确认。
- `tmp\wind-daily\config.json` 已初始化，`enabled=false`；当前用户登录任务尚未安装。

## 真实 SDK 小样本

运行本仓库 `scripts/probe_wind.py`，查询日期为 2026-11-11，使用五所的具体合约：

`AU2612.SHF`、`SC2612.INE`、`M2701.DCE`、`SA701.CZC`、`SI2701.GFE`。

`w.start()` 成功；五次串行 `w.wsd()` 均返回 `open/high/low/close/settle/volume/oi`，源日期全部匹配 2026-11-11，无错误或缺失字段。单次请求约 0.078–0.093 秒，查询间隔 1 秒。结果仍标记为 `vendor_native; normalization not yet approved`，不作为生产数据口径认证。

忽略目录中的诊断文件为 `tmp\wind-canary\20261112-082225-351010.json`。探针没有发布或推送数据，且未停止、注销或取消订阅 Wind 会话。

## 当前采集状态与边界

- `scripts/run_wind_daily.py --plan` 识别出 2026-11-11 的期货和核心期权已齐全；当前计划缺口为 2026-11-12 的已完成夜盘。
- 夜盘补采尚未启动；生产 `data/` 未因本次验证而改变。
- 首次运行每日入口时，Python 对 `127.0.0.1:8765` 的本机探测返回 Windows `WinError 10013`，采集在任何 Wind 请求前安全阻断。保护检查现已在该明确错误下改查 Windows TCP 监听表：查到监听即等待，确认无监听才允许继续；系统查询失败仍阻断。此端口检查不建立跨仓库请求互斥。
- 本机每日入口仍保持关闭，Windows 登录任务、整批日盘/期权/夜盘采集、历史发布及 Dot 监控均未在这次验证中验收或启用。远端 Actions/Bridge 仍保持关闭，旧 iFinD 采集保持 idle。
