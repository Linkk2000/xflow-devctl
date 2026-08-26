# XFlow devctl 跨平台运行时设计

状态：待书面复核
日期：2026-08-25

## 背景

`xflow-spec` 当前以 GNU Bash 4+ 脚本作为控制中枢，约四十个 Shell 入口包含数组、`BASH_SOURCE`、`mapfile`、正则匹配和大小写转换等 Bash 专有能力。逐文件改写会扩大规范仓的维护面，也容易改变 Linux 用户已经稳定使用的行为。

另一方面，独立的 `xflow-devctl` 已经以 Python 模块承担 Git、Issue、审批、合同和任务状态等主要逻辑，但其 POSIX 入口仍依赖 Bash，部分文件写入 API 依赖 Python 3.10+，入口测试还假定 PowerShell 必然存在。它已经具备成为公共跨平台运行时的基础，却尚未形成可由 `xflow-spec` 复用的稳定承载边界。

本设计把平台兼容集中到 `xflow-devctl`，让 `xflow-spec` 只承担薄入口、平台配置和原有 Linux 脚本。最终结果不是把现有 Shell 全部改写成另一种 Shell，而是保留 Linux 原路径，同时为其他受支持环境提供 Python 原生路径。

## 目标

1. `xflow-spec` 以最小改动同时支持 Linux 和 POSIX 桌面环境。
2. Linux 用户继续使用现有命令、参数、默认路径和 Shell 实现，不改变服务管理与终端行为。
3. POSIX 桌面环境不要求额外安装 GNU Bash 或 PowerShell，使用 Python 3.9+ 执行公共流程。
4. `xflow-devctl` 成为平台无关的 Python 命令核心，平台差异通过能力检测和配置处理。
5. Git、Issue、状态和开发联调命令在两条运行路径上保持兼容的命令合同与退出码。
6. Windows 的既有 PowerShell 入口继续保留，但它不是 POSIX 环境的安装依赖或测试前提。

## 非目标

- 不把 `xflow-spec/_ops/` 下的现有 Shell 文件逐个翻译为 Python 或 zsh。
- 不在第一阶段迁移 `repo new`、`focus/squad`、`seed/e2e`、`loc` 等低频或管理型命令。
- 不改变产品仓业务代码、服务端口、构建工具或运行参数。
- 不把任何消费仓的业务流程、交付机制或业务字段带入 `xflow-devctl`。
- 不要求 Linux 用户初始化新的运行时依赖才能继续使用原有路径。
- 不删除 Windows 的 `devctl.ps1`，也不要求其他平台通过它完成验证。

## 方案选择

### 选定方案：Spec 薄分发器 + 固定版本的 devctl Python 运行时

`xflow-spec` 保留现有 Linux 实现，并通过 `.xflow/ops/devctl` Git submodule 固定 `xflow-devctl` 版本，作为非 Linux 路径的运行时。顶层 `devctl` 只做能力检测和路由，不包含业务命令逻辑。Linux 路径不要求初始化该 submodule；Python 路径若发现 gitlink 尚未初始化，则在产生任何副作用前给出明确命令。

优点：

- `xflow-spec` 的最终维护面很小，现有 Shell 不发生横向重写。
- Linux 回归边界清晰，可以直接对照改造前行为。
- Python 兼容、路径处理、进程管理和测试集中在一个仓库。
- 运行时版本可固定，避免依赖某台机器上偶然存在的全局工具。

代价：

- `xflow-spec` 需要记录一个固定版本的运行时依赖。
- 第一阶段只承诺高频开发闭环，未迁移命令会返回明确的能力缺失提示。

### 未选方案一：继续扩展 GNU Bash 兼容层

该方案可以复用大部分脚本，但 POSIX 桌面环境仍需安装新版 Bash，并继续维护解释器选择、终端实现和 GNU/BSD 工具差异。它没有解决运行时依赖问题，也会继续扩大 `xflow-spec` 的兼容代码。

### 未选方案二：在 Spec 内重写全部脚本

该方案不依赖独立运行时，但会在规范仓复制 Git、Issue、进程管理和平台判断能力，形成第二套实现。改动面和回归成本都明显高于选定方案。

## 总体架构

```text
产品仓 ./devctl shim
          |
          v
xflow-spec/devctl（POSIX 薄分发器）
          |
          +-- Linux 且能力满足 --> 原有 GNU Bash 入口 --> xflow-spec/_ops/*.sh
          |
          +-- Python 路径 ------> 固定版本 xflow-devctl
                                      |
                                      +-- 通用 Git / Issue Python 核心
                                      +-- cockpit profile 解析
                                      +-- 状态与开发编排能力
                                      +-- 平台能力检测与进程适配
```

分发器不依赖 Bash 专有语法。它只解析运行路径所需的最小环境变量，并把原始参数原样传递给选定后端。Linux 路径进入现有 Bash 入口后，其后续行为保持不变。

Python 路径读取 `xflow-spec` 提供的 cockpit profile。profile 只描述仓库拓扑、脚本位置、服务命令、端口和受支持命令，不包含客户或业务逻辑。`xflow-devctl` 根据 profile 提供通用执行能力，不硬编码 XFlow 产品仓名称。

## 仓库边界与文件影响

### xflow-spec

最终目标是只保留以下类型的变化：

1. 顶层 `devctl`：改为短小的 POSIX 分发器。
2. Linux 入口：保存改造前的现有 Bash 路由逻辑，继续调用 `_ops/*.sh`。
3. 运行时绑定：以 `.xflow/ops/devctl` submodule 固定 `xflow-devctl` 版本，并在缺失时给出可复制的初始化命令。
4. cockpit profile：声明工作区布局和 Python 路径可用的命令。
5. 一组薄层合同测试：验证路由选择、参数透传和 Linux 行为不变。

此前为 GNU Bash 解释器选择而加入的广泛平台辅助逻辑、长篇实现计划和重复兼容测试，在新方案落地时应审查并删除或收敛。除 Linux 入口的必要搬移外，原有 `_ops/*.sh` 应恢复为平台改造前的内容，不继续逐文件叠加兼容分支。

### xflow-devctl

允许进行较大范围的内部调整，但公共命令合同必须稳定：

1. POSIX 启动器使用 `/bin/sh` 可解析的语法，按 `DEVCTL_PYTHON`、`python3`、`python` 的顺序选择 Python。
2. Python 运行时最低版本为 3.9；版本不足或依赖缺失时给出明确错误，不静默切换行为。
3. 将换行稳定写入集中到公共 I/O 函数，不依赖 Python 3.10 才支持的 `Path.write_text(..., newline=...)`。
4. 所有路径在比较前采用一致的规范化策略，避免同一目录的不同等价表示导致错误拒绝。
5. 增加可配置的 cockpit profile 和命令适配层；通用核心不依赖某个产品或客户。
6. 进程启动、浏览器打开、终端承载和 Docker 检测使用能力接口，缺少可选能力时返回可操作的错误。
7. PowerShell 入口继续调用同一 Python 核心；POSIX 测试不再无条件要求 PowerShell 可执行文件。

### 产品仓

产品仓不承载平台实现。现有轻量 shim 继续把仓库名和参数转给相邻的 `xflow-spec/devctl`。若 shim 本身只使用系统 Bash 已支持的基础语法，则第一阶段不修改产品仓；验证发现 shim 存在更高版本依赖时，统一更新生成模板，而不是在各产品功能提交中分别修补。

## 命令范围

第一阶段的 Python 路径覆盖：

| 命令 | 行为来源 | 兼容要求 |
|---|---|---|
| `state`、`state show` | cockpit profile + 状态读取器 | 输出关键信息、退出码与现有入口一致 |
| `git start/status/commit-msg/mr/done` | `xflow-devctl` Git 核心 | 保持现有参数；远端写入仍遵守所属仓库门禁 |
| `issue create/list/show` | `xflow-devctl` provider 核心 | 自动识别 Gitee/GitHub；不改变凭证来源 |
| `dev preflight` | 能力检测器 | 报告 JDK、Docker、Node、仓库与缓存状态 |
| `dev docker setup/status` | Docker 能力适配 | 不改变 Linux 的既有 Docker 行为 |
| `dev all` | 工作区进程编排器 | 使用 profile 声明的服务和端口 |
| `run` | 单场景进程编排器 | 保持当前前后端路径和参数合同 |
| `dev playground`、`pg`、`playground` | playground 编排器 | 保持别名、端口和打开浏览器选项 |

第一阶段未覆盖的命令在 Python 路径返回统一错误，明确指出该命令当前仅由 Linux 后端提供，并列出已支持命令。它们不得静默落回不兼容的 Shell，也不得部分执行后失败。

## 配置与发现

分发器按以下顺序发现 Python 运行时：

1. `XFLOW_DEVCTL_ROOT` 显式指定的运行时目录；
2. `xflow-spec` 固定的运行时绑定目录；
3. 找不到时失败并给出初始化或修复命令。

Python 解释器按以下顺序发现：

1. `DEVCTL_PYTHON`；
2. `python3`；
3. `python`。

只有通过版本和依赖预检的解释器才会启动命令。发现顺序是合同的一部分，便于 CI 和本机环境显式覆盖。

cockpit profile 使用仓库内相对路径；运行时加载后统一解析为规范化绝对路径。正式文件不得写入用户目录、机器名称或本机安装路径。

## 数据流

以 `./devctl --repo xflow-web git status` 为例：

1. 产品仓 shim 把 `--repo xflow-web git status` 转给 `xflow-spec/devctl`。
2. 分发器根据运行能力选择 Linux 后端或 Python 后端。
3. Python 后端读取 cockpit profile，解析工作区根和目标仓。
4. Git 核心在目标仓执行只读状态查询。
5. 结果通过标准输出返回，诊断通过标准错误返回，退出码原样传递。

远端写入命令继续由 `xflow-devctl` 的审批和 provider 边界控制。平台路由不得扩大授权，也不得因为使用另一条运行路径绕过 Issue、审批、push 或 MR 门禁。

## 错误处理

- 缺少 Python：退出非零，并列出解释器发现顺序和可用覆盖变量。
- Python 版本不足：打印检测到的版本和最低版本 3.9。
- 运行时绑定缺失：不自动联网下载；提示固定的初始化命令。
- profile 缺失或无效：在启动任何子进程前失败，并指出具体字段。
- 命令尚未迁移：在产生副作用前失败，打印受支持命令列表。
- 可选桌面能力缺失：例如没有可用浏览器打开器或终端承载器时，保留服务进程状态并返回可操作提示；是否继续由具体命令合同决定。
- 子进程失败：保留原退出码，诊断中包含组件名和执行阶段，但不输出 token、密码或环境文件内容。
- 路径不一致：比较前统一规范化；规范化后仍越出工作区边界则拒绝执行。

## 测试策略

### xflow-devctl

1. 在 Python 3.9 和当前稳定 Python 上运行核心测试。
2. POSIX 启动器测试覆盖解释器优先级、参数原样透传、缺失与版本不足错误。
3. 路径测试覆盖等价临时目录表示、包含空格的路径和 worktree Git 路径。
4. PowerShell 测试只在该能力存在时运行；Python 核心测试与平台入口测试分离。
5. cockpit profile 使用临时多仓 fixture 验证 `--repo`、状态读取和进程命令解析。
6. Git 与 Issue 测试继续验证远端写入门禁，平台路径不得新增旁路。

### xflow-spec

1. Linux 基线使用受支持的 GNU Bash 版本运行现有测试，验证命令、默认值和输出合同没有变化。
2. Python 路径在没有额外 GNU Bash、没有 PowerShell 的环境中运行第一阶段命令测试。
3. 路由合同测试验证 Linux 进入原后端，其他受支持环境进入固定 Python 后端。
4. 产品仓 shim 至少选取 Web、Server 和 SDK 各执行一次只读命令，验证参数与仓库根解析。
5. 执行 `git diff --check`，并扫描本机绝对路径、宿主标识、秘密和客户业务耦合。

当前 `origin/main` 的基线测试已暴露两项需要纳入实施的既有问题：路径断言没有先规范化，以及 POSIX 环境的入口测试无条件启动 PowerShell。它们属于平台测试隔离缺口，修复后不能降低 Windows 入口的独立覆盖。

## 实施与提交边界

实施分为两个独立提交序列：

1. `xflow-devctl`：先完成 Python 3.9 兼容和平台独立测试，再实现 profile、编排能力和 POSIX 启动器。
2. `xflow-spec`：在运行时能力验证完成后，收敛既有 Bash 兼容改动，接入薄分发器和固定运行时绑定，并完成 Linux 回归。

两个仓库分别检查、测试和提交。`xflow-devctl` 的通用能力先达到可固定版本状态，`xflow-spec` 才更新绑定；不得让规范仓指向未验证的临时工作区。最终是否 push、创建远端 Issue 或合并请求仍遵守各仓当时有效的人工门禁。

## 验收标准

1. Linux 用户无需改变命令或安装依赖即可继续使用原有 Shell 路径。
2. POSIX 桌面环境在 Python 3.9+ 下无需新版 GNU Bash 或 PowerShell，能够执行第一阶段全部命令。
3. `xflow-spec` 不再维护逐脚本的平台分支；最终平台新增文件和长期维护点被限制在薄入口、profile、运行时绑定和合同测试。
4. `xflow-devctl` 的 Python 核心不包含消费方专有的交付机制或业务逻辑。
5. 两仓聚焦测试、Linux 回归、Python 3.9 回归和 `git diff --check` 全部通过。
6. 正式 diff、文档和提交信息不包含本机安装路径、秘密或单一宿主背景。
