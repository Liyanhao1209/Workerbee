# Workerbee

集成多 agent harness 的跨平台多 agent 协作 workflow 框架。

用户用可编辑拓扑描述长程任务的执行阶段，为各阶段选择模型、harness 和工具，
再向 Workflow 提交任务。Workerbee 自动管理会话、依赖推进、结果交接、后台执行、
人工干预和异常恢复——用户不必逐阶段创建 session、查找命令和复制摘要。

## 状态

首个实现版本，目标平台 Linux。Web GUI 为验收入口。

## 文档

`docs/` 下为需求与设计基线，实现以其为准：

| 文档 | 角色 |
| --- | --- |
| `Workerbee_proposalv0.01.pdf` | P｜原始功能意图 |
| `workerbee_v0.01_audit.docx` | A｜审计报告（问题线索与候选方案） |
| `Workerbee_audit_v0.01_review.pdf` | R｜作者澄清（**优先级最高**） |
| `Workerbee_功能需求清单_v0.02.pdf` | 需求基线：WF/ACT/CFG/HAR/AUTH/EXT/RUN/DATA/OBS/HUM/LIFE/REC/RES/TPL/AI/UI/PLAT + AC-01–AC-22 |
| `Workerbee_架构设计_v0.02_py.md` | 设计基线：分层、数据模型、机制、D-01–D-13 定案 |

依据优先级：**作者澄清（R）＞ 功能清单（v0.02）＞ 审计建议（A）**。
两者冲突时以清单为准。

## 分层

```
L6 客户端层      Web Client（主力） / Terminal Client（可选）
L5 安全治理层    Secret Store / Approval Gateway / AI 写闸门 / 脱敏
L4 数据与事件层  Artifact Store / Message Bus / Event Log / Context Assembler / Summarizer
L2 运行时内核    Scheduler / State Machine / Queue / Lease / Reconciler / Resource Ledger / Cancel
L3 适配层        HarnessAdapter 六组契约 / Adapter Host / Session Supervisor
L1 定义层        Workflow / Revision / Node / Edge / Profile / Template / Registry / 校验管线
```

进程：`workerbee-supervisor`（持 harness 子进程）· `workerbee-core`（L1/L2/L4/L5）·
`workerbee-web`（静态资源 + BFF）。

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # 全部测试
.venv/bin/pytest -m unit              # 单元测试
.venv/bin/pytest -m scenario          # AC-xx 验收场景
.venv/bin/pytest -m "not interactive" # 跳过需要真实 harness 的用例
```

## 运行

```bash
.venv/bin/workerbee-core          # 内核（API + 调度）
.venv/bin/workerbee-supervisor    # 会话托管
.venv/bin/workerbee               # CLI
```

## 边界（首版明确不做）

不自动评价模型强弱；不建强制 token／费用预算；不支持跨 Workflow 的任务依赖与消息互通；
不恢复用户主动删除的任务或 Workflow；不从 Skill 自由文本推导操作系统级资源硬限制；
不支持 ANY／K-of-N 汇聚、自动多轮返工与任意循环拓扑。上述边界在数据模型中
「字段预留、行为关闭」，而非实体缺席。
