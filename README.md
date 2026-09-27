# Workerbee

集成多 agent harness 的跨平台多 agent 协作 workflow 框架。

用户用可编辑拓扑描述长程任务的执行阶段，为各阶段选择模型、harness 和工具，
再向 Workflow 提交任务。Workerbee 自动管理会话、依赖推进、结果交接、后台执行、
人工干预和异常恢复——用户不必逐阶段创建 session、查找命令和复制摘要。

## 当前状态

首个实现版本。**已在本机用真实 harness 跑通跨 harness 主闭环**：
Claude Code 规划 → Kimi Code 执行 → Claude Code 复核，一次提交自动建会话、
交接产物、推进依赖直至成功，用量与产物可在 Web 界面回看。

目标平台 Linux（D-13 的其余平台未验证）。

## 快速开始

```bash
# 1) 安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2) 检查环境（会告诉你 claude / kimi 在不在、数据目录能不能写）
.venv/bin/workerbee doctor

# 3) 启动内核（默认只监听 127.0.0.1）
.venv/bin/workerbee core --data-dir .workerbee

# 4) 浏览器打开（内核会打印访问令牌）
#    http://127.0.0.1:8765
```

生产部署建议**另起一个进程托管 harness 会话**，这样内核重启不会打断正在跑的任务：

```bash
.venv/bin/workerbee supervisor --data-dir .workerbee   # 一个终端
.venv/bin/workerbee core --data-dir .workerbee --use-supervisor
```

界面顶栏会显示当前是 `supervisor` 还是 `in_process` 托管。**后者会在内核重启时
打断在跑的任务**——界面会明确警示，不要等到丢任务才发现。

## 分层

```
L6 客户端层      Web 客户端（React + React Flow）
L5 安全治理层    Secret Store / Approval Gateway / AI 写闸门 / 脱敏 / 出站扫描
L4 数据与事件层  产物存储 / 消息总线 / 事件日志 / 上下文组装 / 摘要 / LLM 后端
L2 运行时内核    调度 / 状态机 / 队列 / 租约 / 对账 / 资源台账 / 取消协议
L3 适配层        HarnessAdapter 六组契约 / Adapter Host / Session 托管
L1 定义层        Workflow / Revision / Node / Edge / Profile / Template / 注册表 / 校验管线
```

三个进程：`workerbee-supervisor`（持有 harness 子进程）· `workerbee-core`（L1/L2/L4/L5）·
`workerbee-web`（静态资源，由 core 一并托管）。

## 架构上的几条硬纪律

这些不是风格偏好，是踩过才知道疼的地方：

- **有效图是派生的，不是存的。** `derive()` 是纯函数，同样的 enabled 集合必得同样的
  有效图。启停不会留下「短路边」——因为根本没有地方可留。
- **控制操作只写 `desired_state` 并 bump `control_epoch`。** 执行器收敛后写
  `observed_state`。所有状态迁移都带 CAS，失败的一方读取「实际生效结果」而不是覆盖。
- **先登记后使用，清理只信台账。** 资源创建前先进台账；所有清理路径遍历台账执行，
  不依赖内存状态。删文件只对托管目录内生效——宁可留一个可见的未清理句柄，
  也不能删到用户的项目文件。
- **未知 ≠ 零，也 ≠ 没有。** 用量取不到记 `None`（未知）而不是 0；会话台账查不到
  时报「查不到」而不是空列表；事件缓冲丢了报「有缺口」而不是给一段残缺的历史。
- **失败显式化。** 摘要未覆盖契约要点就是交接失败，不静默截断；不支持的能力
  如实声明为不支持，不声称支持却卡死。
- **权限不做隐式默认。** 没有权限钩子的 harness 仍可用，但用户必须**显式**选定
  一个不会询问的权限模式；框架绝不替用户默认放行。

## 目录

```
src/workerbee/
  core/domain/      实体与状态机        core/graph/       derive 与校验管线
  core/runtime/     调度、生命周期、取消、对账   core/resources/   台账与清理器
  data/             SQLite、产物、消息、事件、上下文组装、摘要、LLM 后端
  adapters/sdk|host|mock|claude_code|kimi_code
  security/         Secret Store、脱敏、出站扫描、审批网关
  supervisor/       Session 托管进程
  server/           FastAPI 网关（REST + WebSocket）
  app.py            组合根（唯一可以同时 import 各层的模块）
web/                React + TypeScript 客户端
tests/              unit / integration / fuzz / scenarios / interactive
```

## 测试

```bash
.venv/bin/python -m pytest                      # 全部（不含需要真实 harness 的）
.venv/bin/python -m pytest -m unit              # 单元
.venv/bin/python -m pytest -m scenario          # AC-01–AC-22 验收场景
.venv/bin/python -m pytest -m interactive tests/interactive   # 真实 harness（慢、消耗配额）
.venv/bin/python scripts/gui_smoke.py --token <TOKEN>         # 无头浏览器过一遍每个页面
```

`gui_smoke.py` 会打开每个页面、收集控制台错误、截图到 `.workerbee/gui-smoke/`。
它只能回答「有没有白屏、有没有 JS 报错、关键元素在不在」，回答不了「交互顺不顺手」。

## 文档

`docs/` 下为需求与设计基线：

| 文档 | 角色 |
| --- | --- |
| `Workerbee_proposalv0.01.pdf` | P｜原始功能意图 |
| `workerbee_v0.01_audit.docx` | A｜审计报告（问题线索与候选方案） |
| `Workerbee_audit_v0.01_review.pdf` | R｜作者澄清（**优先级最高**） |
| `Workerbee_功能需求清单_v0.02.pdf` | 需求基线（WF/ACT/CFG/RUN/DATA/OBS/LIFE/REC/RES/… + AC-01–AC-22） |
| `Workerbee_架构设计_v0.02_py.md` | 设计基线（分层、数据模型、机制、D-01–D-13 定案） |

依据优先级：**作者澄清（R）＞ 功能清单 ＞ 审计建议（A）**。

## 首版明确不做

不自动评价模型强弱；不建强制 token／费用预算；不支持跨 Workflow 的任务依赖与消息互通；
不恢复用户主动删除的任务或 Workflow；不从 Skill 自由文本推导操作系统级资源硬限制；
不支持 ANY／K-of-N 汇聚、自动多轮返工与任意循环拓扑。

另外这些是**已知缺口**（不是设计边界，是还没做）：AI 建图、Graph Capture、
Base Assistant（M3 批次）；Windows/macOS 平台适配；`SecretStore` 的 revoke/rotate
尚未写审计事件。
