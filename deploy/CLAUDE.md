# L2 模块地图

deploy/
L2 | 父级: ../CLAUDE.md

服务定义不含机器相关值。`install_service.py` 在目标机器上推导解释器、项目根、数据目录与端口。
生成物写入 `generated/`，该目录不入库。脚本不执行 `launchctl` 或 `systemctl`。

## 成员清单

install_service.py: 安装前端。用 `string.Template` 渲染模板，打印需要在系统终端执行的命令。不改动系统状态。
launchd.web.plist.tmpl: macOS 工作台常驻单元。`KeepAlive` 保活，日志写入数据目录。
launchd.scheduler.plist.tmpl: macOS 每日 20:00 一次性调度单元。不设 KeepAlive。
systemd.web.service.tmpl: Linux 用户级工作台单元。失败自动重启。
systemd.scheduler.service.tmpl: Linux 一次性推送单元。由 timer 触发。
systemd.scheduler.timer.tmpl: Linux 每日 20:00 触发器。`Persistent` 补跑错过的唤醒。

# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
