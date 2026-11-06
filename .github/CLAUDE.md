# L2 模块地图

.github/
L2 | 父级: ../CLAUDE.md

CI 是发布契约的执行者。这个目录没有业务代码，只有构建门禁与卫生门禁。

执行环境是兼容性契约的一部分。Python 3.9 固定跑 `ubuntu-22.04`。`ubuntu-latest` 已漂移到 26.04，该镜像不再提供 3.9 预编译包。改测试矩阵前，先确认目标镜像提供该 Python 版本。

## 成员清单

workflows/ci.yml: 三 job 工作流。`offline-tests` 在四个 Python 版本跑离线检查、GEB 契约与版本一致性；`package` 构建 wheel 并从 wheel 安装验证入口与静态页；`hygiene` 扫描密钥与个人数据。

# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
