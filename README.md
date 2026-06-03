# _ops — devctl 实现目录

`devctl` 是薄入口，本目录存放全部实现脚本。

```
_ops/
  lib/common.sh       # 公共库：git / Gitee API / 提交信息启发式
  git/
    start.sh          # 干净工作区 → 功能分支
    commit-msg.sh     # 生成 / 提交 conventional message
    mr.sh             # 推送 + Gitee Pull Request
    done.sh           # 合并后清理本地分支
    status.sh         # 分支 / issue / PR 元数据
  issue/
    create.sh | list.sh | show.sh
  run.sh              # 仅前端仓库：联调启动
  help.txt
  .run/               # run 命令运行时日志与 pid（gitignore）
```

配置：`~/gitee.env.local` 或环境变量 `GITEE_TOKEN`。
