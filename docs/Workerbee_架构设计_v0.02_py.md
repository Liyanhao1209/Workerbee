# Workerbee 架构设计 v0.02

**文档性质**：设计层架构方案（Design Proposal）。本文只负责结构、机制与接口的设计决策，不包含可实现的生产代码；伪代码仅用于精确表达算法语义。

**需求基线**：《Workerbee 功能需求清单 v0.02》（下称「清单」）。本文引用的 WF-xx / ACT-xx / CFG-xx / HAR-xx / AUTH-xx / EXT-xx / RUN-xx / DATA-xx / OBS-xx / HUM-xx / LIFE-xx / REC-xx / RES-xx / TPL-xx / AI-xx / UI-xx / PLAT-xx / D-xx / AC-xx 编号均以该清单为准。

**依据优先级**：作者澄清（R）＞ 功能清单（v0.02）＞ 审计建议（A）。凡采纳审计报告中的机制（如资源台账、两级取消、derive 派生），本文将其作为设计决策重新论证，而非默认继承；凡 R 与 A 冲突之处（删除语义、节点队列归属、session 模型、context_len 语义、能力标签与预算），以 R 为准。

**术语约定**（与清单第 2 节对齐，括号内为本文类名用词）：

| 用户可见术语 | 设计实体 | 说明 |
| --- | --- | --- |
| Workflow | WorkflowDefinition + WorkflowRevision | 可复用的流程定义；修订版本不可变 |
| 节点 | NodeDefinition | 流程中的逻辑阶段 |
| 任务 | Task | 用户向 Workflow 的一次主动提交（对应审计稿的 WorkflowRun） |
| 节点阶段 | TaskStage | 某任务在某节点上的执行部分（对应 TaskInstance） |
| 执行尝试 | Attempt | 一次节点阶段的首次执行、重试或候选切换 |
| Session | SessionHandle | harness 管理的实际会话，由适配层托管 |
| 定义图 / 有效图 / 本次执行记录 | DefinitionGraph / EffectiveGraph / ExecutionSnapshot | 功能语义区分；实现为一份边表 + enabled 标志 + 发射时钉扎快照，不是三张物理图 |

---

## 1. 设计目标与总体原则

### 1.1 设计目标

使清单第 3 节的全部功能要求可以被实现、被验收（AC-01–AC-22），并使 D-01–D-13 全部得到显式定案。设计成功的判据不是「类图画得完整」，而是：每一条功能编号都能映射到具体的层、模块与实体（见第 13 章），每一个待决项都有「选择／理由／用户影响／验证场景」四要素（见第 14 章）。

### 1.2 总体原则

1. **单一事实源，引用与派生替代副本。** 拓扑的唯一事实源是定义层的边表；节点启停的唯一事实源是节点的 enabled 标志；有效图永远是派生结果，不持久化、不接受直接编辑。凭据的唯一事实源是 Secret Store，其他位置只存引用。
2. **定义与运行分离，发射即钉扎。** 用户对定义图与配置的每一次有效修改产生新的 revision；任务发射时钉扎 revision 与有效图版本，在途任务按钉扎版本执行至结束（WF-06、CFG-07）。
3. **意图与观测双轨。** 所有运行时对象区分 desired_state（用户或系统控制意图）与 observed_state（系统观测到的事实）；控制操作只写 desired_state 并携带单调递增的 control_epoch，执行器据此收敛（OBS-01、LIFE-01、AC-11）。
4. **先登记后使用，清理只信台账。** 一切运行资源（进程、流、连接、临时文件、端口、锁、浏览器句柄）创建前先登记资源台账；所有清理路径——完成、失败、暂停、删除、停用、崩溃恢复——遍历台账执行，不依赖内存状态（RES-01/02、Concern 2）。
5. **不可信输入一律过闸门。** AI 生成的拓扑、Graph Capture 的产物、第三方适配器上报的事件，均以草稿身份进入，经统一校验管线并显式确认后生效（WF-02/03、HUM-05、AC-16）。
6. **失败显式化，禁止静默。** 校验失败、交接失败、整理失败、恢复失败、清理失败都必须有可见状态与原因，不得以静默降级、静默截断、静默跳过换取「成功」（DATA-03、CFG-05、REC-04、LIFE-06）。

### 1.3 明确的非目标（与清单 1.3 对齐）

不自动评价模型能力强弱；不建立强制 token／费用预算系统；不支持跨 Workflow 的任务依赖与消息互通；不恢复用户主动删除的任务或 Workflow；不从 Skill 自由文本推导操作系统级资源硬限制；第一版不支持 ANY／K-of-N 汇聚、自动多轮返工与任意循环拓扑。上述边界在数据模型中体现为「字段预留、行为关闭」，而非实体缺席。

---

## 2. 总体分层架构

系统分为六层。层的划分标准是「事实源的归属」而非部署边界；每层只通过显式接口与邻层交互。

```text
┌─────────────────────────────────────────────────────────────┐
│ L6 客户端层  Web Client（主力） / Terminal Client（可选）      │
│    纯视图层：编辑、监控、审批、恢复、清理入口；不持有权威状态     │
├─────────────────────────────────────────────────────────────┤
│ L5 安全治理层  Secret Store / Approval Gateway / AI 写闸门     │
│    凭据唯一存储；审批闭环；AI 产物的 diff 预览与确认             │
├─────────────────────────────────────────────────────────────┤
│ L4 数据与事件层  Artifact Store / Message Bus / Event Log /   │
│    Context Assembler（ContextPackage 组装器）/ Summarizer      │
├─────────────────────────────────────────────────────────────┤
│ L2 运行时内核  Scheduler / State Machine / Queue / Lease /     │
│    Reconciler / Resource Ledger / Cancel Protocol             │
├─────────────────────────────────────────────────────────────┤
│ L3 适配层  HarnessAdapter 契约（六组） / Adapter Host /        │
│    Session Supervisor（会话托管进程）                          │
├─────────────────────────────────────────────────────────────┤
│ L1 定义层  Workflow / Revision / Node / Edge / Profile /      │
│    Template / Skill / Tool / Credential / Harness Registry +   │
│    Validation Pipeline（校验管线）                             │
└─────────────────────────────────────────────────────────────┘
        底层：本地 OS 进程、harness CLI、模型 API、MCP server
```

各层职责与关闭的功能编号：

| 层 | 职责 | 唯一事实源 | 主要承接 |
| --- | --- | --- | --- |
| L1 定义层 | 流程、节点、依赖、执行候选、模板、工具、凭据引用的定义与校验 | 边表 + Revision 序列 | WF-01–07、ACT-01/02、CFG-01/02、TPL-01–03、AUTH-01、EXT-01–03、HAR-01 |
| L2 运行时内核 | 任务发射、调度、状态机、暂停/删除/恢复、资源台账、崩溃对账 | Task / TaskStage 表（desired/observed 双轨 + control_epoch） | RUN-01–07、LIFE-01–06、REC-01–05、RES-01/02、OBS-01 |
| L3 适配层 | 统一 harness 生命周期、输出解析、事件流、权限事件转译、能力声明；会话托管 | Adapter Manifest + Session 台账 | HAR-01–03、HUM-01/02、REC-02、PLAT-01 |
| L4 数据与事件层 | 产物存储、消息传递、上下文组装、摘要、事件日志、血缘 | Artifact Store（内容寻址、不可变）+ append-only Event Log | DATA-01–06、CFG-04/05、OBS-02–04 |
| L5 安全治理层 | 凭据存储与引用化、审批闭环、AI 写操作闸门、出站脱敏 | Secret Store + Approval 记录 | AUTH-02、HUM-03–05、AI-01/02、UI-02 |
| L6 客户端层 | 拓扑编辑、监控、审批、恢复与审计视图 | 无（写操作带 revision 做乐观并发） | UI-01–03、OBS-05 |

唯一允许的跨层共享状态是**资源台账**（L2 写入、L3 上报、L5 审计读取），因为资源的发放、持有与审计天然需要同一本账。

---

## 3. 进程与部署视图

单机部署，三个 OS 进程，刻意分离以回答审计指出的自指性问题（Workerbee 自身不能复现「断连即失」）：

| 进程 | 内容 | 崩溃影响 |
| --- | --- | --- |
| `workerbee-supervisor` | Session 托管：持有全部 harness 子进程、SessionHandle 映射、心跳租约；持久化 session 台账 | web/core 崩溃不影响在跑 session；supervisor 自身崩溃由 OS 级守护（systemd/launchd/Windows Service，见 D-13）拉起并对账 |
| `workerbee-core` | L1/L2/L4/L5：API 服务、调度器、状态机、事件日志、Artifact Store、审批网关 | 重启后从持久化状态对账恢复（REC-03）；期间 supervisor 维持 session 存活 |
| `workerbee-web` | 静态资源 + BFF，默认绑 loopback + 访问令牌（UI-02） | 纯视图层，可随时重启 |

适配器以**子进程插件**形式运行（JSON-RPC over stdio），与 core 隔离：第三方适配器崩溃不拖垮内核，权限事件与心跳走同一通道。适配器协议版本化（见 D-09）。

设计说明：不采用 tmux 等外部会话持久化方案（R §2.2.3 已澄清 tmux 仅是举例），session 的存活性由 supervisor 直接持有子进程保证，恢复依赖 harness 自身能力（resume 命令、checkpoint），能力差异由适配层如实上报（HAR-02、D-07）。

---

## 4. 核心机制：三张图与节点启停

本章关闭 ACT-01–ACT-04 与 WF-05，并对清单第 4 节「停用算法」的保留意见给出回应：本文不照搬审计报告的全部用例结论，而是把可证明的性质（无环、幂等、可逆）与需要语义裁决的性质（输入衔接）分开处理。

### 4.1 三层图语义

- **定义图 G₀**：用户在某 revision 中保存的节点集 V 与边集 E₀。发布前必须过校验管线（4.4），G₀ 恒为 DAG。E₀ 永不被启停操作修改。
- **有效图 G_eff = derive(G₀, enabled)**：纯函数派生，按需计算、可缓存，永不持久化为可编辑对象。同样的定义图与 enabled 集合必得同样的有效图（ACT-01 的「不随操作历史漂移」由构造保证）。
- **本次执行记录**：任务发射时钉扎 (revision_id, effective_graph_version) 与有效边集快照，任务的依赖推进、历史回看、恢复全部以该快照为准（WF-06、OBS-03、REC-03）。

### 4.2 derive() 算法

对每个 enabled 节点 v，沿 E₀ 的入边向上游走，跳过 disabled 节点，取每条路径上遇到的第一层 enabled 祖先，作为 v 的有效上游。边集为集合语义，天然去重。

```pseudo
function derive(G0, enabled) -> EffectiveGraph:
    E_eff = {}
    for v in G0.nodes where enabled[v]:
        for u in G0.nodes where enabled[u] and u != v:
            if exists_path_in_G0(u, v) such that
               all intermediate nodes on the path are disabled:
                E_eff.add(u -> v)          # 集合语义：重复路径不重复计数
    return EffectiveGraph(nodes = enabled_nodes(G0), edges = E_eff)
```

性质（可由实现方以随机启停 fuzz 回归验证，对应 AC-05）：

1. **无环**：E_eff 中的每条边在 G₀ 中可达，E_eff ⊆ Reach(G₀)；G₀ 为 DAG ⇒ G_eff 恒为 DAG。「需要回路检查？」在设计层面消解——校验管线保证 G₀ 无环，派生保证 G_eff 无环。
2. **幂等且与操作顺序无关**：derive 是纯函数，任意启停序列交错后，相同 enabled 集合得到相同 G_eff。
3. **可逆**：重新 enable 精确恢复原始依赖，无短路边残留、无节点丢失。
4. **入口语义**：有效入口 = 有效上游为空的 enabled 节点；入口停用后若存在新的合法入口，任务照常发射（ACT-02）；全部停用或无可执行节点时拒绝提交（WF-05）。

对清单第 4 节保留意见的回应：审计用例中的 X→Q、Y→P「跨依赖」在传递闭包意义上本就属于 G₀ 的可达关系，derive 把它们物化为有效边。它**不引入环、不造成重复执行**（集合语义 + 依赖计数去重，ACT-03），但可能改变「谁的数据流向谁」。因此这不是纯图论问题，由 4.3 的衔接规则处理。

### 4.3 绕过节点的数据衔接（ACT-03）

停用 B 后，C 的有效上游变为 A，但 C 的输入契约可能要求的是「B 加工后的结果」。derive 只解决依赖结构，不冒充解决数据语义。规则如下：

1. 每条边可声明可选的**输出契约**（EdgeContract.output，结构化字段清单，见 7.4）。停用操作发生后、任务发射前，校验管线检查：下游节点声明的必需输入，是否能由其**有效上游**的输出契约满足。
2. 无法满足时（C 需要 B 的产物类型而 A 不提供），该校验项失败并在 UI 定位到具体节点与边：用户可选择重新启用 B、修改 C 的输入要求，或显式确认「以 A 的原始输出降级继续」。不允许无提示地把 A 的输出当作 B 的结果。
3. 未声明契约的边回退为文本交接，不做机器校验——这是能力边界，UI 上如实标注。

### 4.4 校验管线（WF-05）

四种建图入口（手动、AI、Graph Capture、模板）的产物统一以草稿身份进入同一条校验管线，发布为 revision 前必须通过：

| 校验项 | 失败处理 |
| --- | --- |
| 无环、无自环、无悬空边引用 | 拒绝发布，定位到边 |
| 存在至少一个有效入口与有效出口 | 拒绝发布，定位到节点 |
| 每个可运行节点至少一组执行候选，且引用存在（harness、凭据、工具均已注册） | 拒绝发布，标出待配置项（AI 建图时缺失项标为「待配置」，禁止编造，WF-02） |
| 停用任意子集后的可执行性预检（有效图仍有入口、必需输入可衔接） | 在启停操作前预览（ACT-02），不阻断草稿保存 |
| 执行候选与 harness 能力匹配（compact、审批、后台工作等关键能力） | 不支持关键能力的组合不能以「完整支持」状态接受（HAR-02） |
| 草稿态与已发布态区分 | 草稿可保存不完整内容，但不能发射任务（WF-01） |

---

## 5. 数据模型

本章给出主要类与字段。字段表是**设计基线而非穷举**：实现阶段可增列内部字段，但不得删除本文声明语义的字段，不得把派生数据提升为可编辑事实源。所有实体含 `created_at`、`updated_at`，不再逐条列出。

### 5.1 L1 定义层

**WorkflowDefinition**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| workflow_id | uuid | 主键 |
| name / description | text | 用户命名与描述 |
| current_revision_seq | int | 指向当前已发布修订 |
| status | enum(draft, published, archived, deleted) | deleted 为逻辑删除，定义与历史保留供回看（LIFE-05） |
| max_concurrent_tasks | int，默认有界 | 该 Workflow 并发任务上限（容量不足时的背压入口，D-03） |

**WorkflowRevision**（不可变快照，任何有效编辑产生新 revision）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| revision_seq | int | 单调递增 |
| workflow_id | ref |  |
| source | enum(manual, ai_generated, graph_capture, template) | 建图入口留痕（AC-16） |
| draft_of | ref/null | AI／Capture 产物的草稿来源与 diff 基础（HUM-05） |
| effective_graph_version | int | 由 enabled 集合与边集哈希派生；发射时钉扎 |

**NodeDefinition**（挂在 revision 下）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| node_id | uuid | 跨 revision 稳定，供历史归因 |
| name / role / description | text | role 用于上下文组装（7.3） |
| enabled | bool | 启停唯一事实源；启停操作只翻转此字段并 bump revision |
| system_prompt | text / null | 可选节点系统 prompt（CFG-06） |
| profiles | list\<ExecutionProfile\>（有序） | 执行候选，有序即优先级；取代 v0.01 三个 Preferences 数组的下标对齐 |
| skill_refs | list\<ref + version\> | 引用工具库或节点级临时 Skill（EXT-01） |
| tool_refs | list\<ref + version\> | 引用 MCP 工具（EXT-02） |
| ui_position | (x, y) | 前端布局，纯展示 |

**ExecutionProfile**（执行候选；CFG-01–05）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| profile_id | uuid | 对齐键；重排、删除候选不产生错配 |
| model_name | text | 系统不评价强弱，只做兼容性检查（清单 1.3） |
| harness_ref | ref HarnessRegistration | 模型与 harness 解耦，多对多可表达 |
| credential_ref | ref Credential | 只存引用，永不内嵌凭据（AUTH-02、TPL-03） |
| reasoning_effort | enum / null | 以适配器验证的能力为准；不可用参数提示而非静默忽略（CFG-02） |
| retry | { max_attempts, backoff_base_ms, backoff_cap_ms, retryable_errors[] } | 有界重试；错误分类见 D-05 |
| compact_threshold | int / null | 用户期望触发上下文整理的阈值，**非模型最大窗口**（R §1.3.2 澄清）；实际触发取 min(用户阈值, harness 实际上限)（CFG-04） |

**Edge**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| from_node / to_node | ref | 拓扑唯一事实源 |
| output_contract | json / null | 可选的输出结构声明，供 4.3 的衔接校验与交接校验使用 |
| desc | text / null | 关系描述，可 AI 生成；展示用途，不作机器判据 |

**Template**（TPL-01/02/03）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| kind | enum(workflow, node) | 流程模板 / 节点模板 |
| payload | json | 拓扑 + 配置快照；序列化时强制剥离凭据为引用占位，实例化时要求重绑定 |
| excludes | 固定规则 | 不含任何运行时内容：队列、活跃 session、审批状态、执行结果 |
| version / source_revision | — | 模板编辑不静默改写已创建流程；同步更新需 diff + 显式应用 |

**SkillDoc**：{ skill_id, name, content, version, scope(global / node_local) }。Skill 是执行指导，UI 与文档中不得显示为「框架已强制的资源限制」（RES-04、AC-22）。

**ToolSpec**（EXT-02）：{ tool_id, name, kind(mcp), launch { command, args, env, transport }, io_schema, risk_level, approval_policy(auto/ask/deny), version, health_check }。

**CredentialRef**：{ credential_id, label, kind(api_key / oauth / base_url_pair / harness_login), secret_locator }。secret_locator 指向 Secret Store；凭据本体不出 L5。

**HarnessRegistration**（HAR-01）：{ harness_id, adapter_id, adapter_version, exec_path, env_template, auth_binding(credential_ref 或本机登录态), capabilities_snapshot }。

### 5.2 L2 运行时内核

**Task**（任务；对应审计稿 WorkflowRun）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| task_id | uuid | 系统生成；两次有意提交相同内容 = 两个任务（RUN-02） |
| idempotency_key | text，UNIQUE(workflow_id, key) | 同一请求的网络重送命中原任务，不产生额外任务（RUN-02） |
| workflow_id, revision_seq, effective_graph_version | — | 发射时钉扎 |
| graph_snapshot | json | 钉扎的有效边集与节点配置快照，恢复与历史回看的依据 |
| input_payload | json | 提交输入 |
| desired_state / observed_state | enum | 双轨；迁移见 6.2 |
| control_epoch | int | 单调递增，控制操作 CAS 仲裁（AC-11） |
| priority | int 0–100，默认 50 | 任务级基础优先级 |
| failure_summary | json / null | 失败/受阻原因与尚在运行的分支（RUN-07） |

**TaskStage**（节点阶段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| stage_id | uuid |  |
| task_id, node_id | ref |  |
| desired_state / observed_state | enum | 状态集见 6.2 |
| queue_order | (node_priority, task_priority, enqueued_at) | 节点级排序键（RUN-04） |
| node_priority | int | 用户在该节点投影上的调序结果；只影响本节点队列，不破坏依赖 |
| current_attempt_seq | int |  |
| blocked_reason | text / null | 依赖未满足/上游失败/输入不足的具体原因 |
| origin_of_control | { op, from_node_id, at } / null | 暂停/删除的发起位置与时间（OBS-03） |

**Attempt**（执行尝试）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| attempt_id, stage_id, attempt_seq | — | 首次执行、重试、候选切换各占一条 |
| profile_id | ref | 实际采用的候选（CFG-07 可追溯） |
| session_ref | ref SessionHandle | 本次尝试使用的会话 |
| lease_id, lease_expires_at | — | 心跳租约；supervisor 定期续期 |
| generation | int | 代次号；清理后递增，迟到回调按代次丢弃（REC-05） |
| usage | { input_tokens, output_tokens, cost_estimate, … } / null | 可取得则记录，不可得标「未知」而非零（OBS-04） |
| outcome | { class(success/retryable_error/fatal_error/user_cancelled), detail } | 错误分类供重试决策（D-05） |
| compact_events | list | 整理发生的次数与效果记录（CFG-04） |

**节点 Pending List 的实现口径**（R §3.2.1 澄清的落地）：权威队列在 Task/TaskStage 表中持久化；节点视图是按 node_id 过滤 + queue_order 排序的**只读投影**；用户在节点投影上的调序写回 node_priority，与派发并发时以 control_epoch CAS 仲裁并返回实际生效结果（RUN-04、AC-03）。

**ResourceRecord**（资源台账，RES-01）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| resource_id | uuid |  |
| owner | (task_id, stage_id, attempt_id, node_id) | 归属键；共享资源记录引用计数 |
| kind | enum(process, api_stream, mcp_conn, tmp_file, port, lock, browser_handle, session) |  |
| locator | json | 定位信息（pid、端口号、路径等） |
| state | enum(open, closing, closed, teardown_failed, orphaned) | teardown_failed 与 orphaned 必须可见并可继续处理（RES-02、LIFE-06） |
| teardown | { method, timeout_ms, escalate } | 关闭方法与升级路径 |

### 5.3 L3 适配层

**SessionHandle**：{ session_ref, harness_id, owner_attempt, created_at, last_heartbeat, persist_locator, capabilities_used }。台账持久化于 supervisor，core 重启后据此重接管（REC-03）。

**AdapterManifest**：{ adapter_id, version, protocol_version, harness_family, capabilities { create_session, resume_session, read_output, interact, interrupt, stop, compact, permission_hook, background_tasks }, platform_matrix, auth_modes }。capabilities 是接口支持情况的声明，不是模型能力评级（清单 §3.4 来源注记）。

### 5.4 L4 数据与事件层

**Artifact**（产物）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| artifact_id / digest | — | 内容寻址；落地即不可变 |
| producer | (task_id, stage_id, attempt_seq) | 来源三元组，下游可辨认（DATA-02） |
| kind | enum(text, file, code, structured) | DATA-01 |
| summary_ref / token_estimate | — | 摘要与估算，供上下文预算 |
| sensitivity | enum(public, internal, sensitive) | 沿血缘传播取最高级，跨 harness/厂商边界前脱敏 |
| lineage | list\<artifact_ref\> | 派生关系；「修改」= 派生新版本（DATA-04） |
| ref_count / tombstoned | — | 引用计数与逻辑删除标记，驱动回收（RES-03） |

**Message**（统一信封）：{ message_id, type(data_ready / feedback / approval_req / approval_resp / state_notify / cancel / btw_input), task_id, from_stage, to_stage, dedup_key, causation_id, generation, payload_ref, created_at }。至少一次投递 + 消费端按 dedup_key 幂等（REC-05）。

**EventRecord**（append-only 事件日志）：{ event_id, ts, scope(workflow/task/stage/attempt/system), type, actor(user/system/adapter/ai), payload, refs }。它是监控（OBS-02）、历史回看（OBS-03）、审批留痕（HUM-04）、崩溃对账（REC-03）的共同数据源；历史落库前经凭据脱敏过滤（AUTH-02）。

**ContextPackage**：不是持久化实体，是每次 Attempt 启动前由组装器生成的中间结构，分区与预算见 7.3。组装输入与结果摘要写入 EventRecord，保证「交接了什么及其来源」可检查（DATA-05）。

### 5.5 L5 安全治理层

**Approval**（HUM-03/04）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| approval_id | uuid |  |
| bound_to | (task_id, stage_id, attempt_id, revision_seq) | 审批精确绑定执行尝试与配置版本；尝试失效或命令内容改变后旧批准作废 |
| action / target / risk | — | 操作内容、目标、可取得的风险信息（不要求另造风险分类器，清单 §3.8 注记） |
| status | enum(pending, approved, denied, expired, undeliverable) | 断连、超时、重复通知不构成批准；回传失败可见 |
| timeout_policy | enum(deny_pause)（默认） | 超时拒绝该动作并将阶段置为显式等待态，释放 stream 资源 |
| decision | { by, at, modified_action? } | 用户可修改后批准；决定回注原请求方 |

**SecretStore**（接口，非字段表）：put / get_by_locator / revoke / rotate。实现建议见第 15 章。约束：凭据不进 prompt、不进模板正文、不进普通日志与无关下游输入（AUTH-02）。

---

## 6. 运行时内核

### 6.1 发射与依赖推进（RUN-01/05/06/07）

1. 提交入口校验幂等键：命中已有 (workflow_id, idempotency_key) 则返回原 task_id。
2. 钉扎 (revision_seq, effective_graph_version) 与 graph_snapshot；校验钉扎版本下存在有效入口；为每个节点创建 TaskStage（初始 WAITING_DEPS 或 READY）。
3. 入口阶段进入就绪集。调度循环按 6.3 领取就绪阶段，驱动 Attempt。
4. 阶段完成判据（RUN-06）：适配器报告会话正常结束 **且** 要求的产出物已通过边契约校验（未声明契约的边退化为「产出物存在」）**且** 无影响结果的后台工作未结束。三者缺一，不发出完成事件；仍有后台工作时显示其关联与状态。
5. 依赖推进：阶段成功后向上游产物写入 Artifact Store，向下游发 data_ready（携带产物引用）。下游阶段的全部**必需**有效上游在同一 task_id 下成功且输入可用，方可转为 READY——第一版汇聚语义即「全部必需上游成功」（清单 §3.5），join 计数按 task_id 隔离，禁止跨任务错配（DATA-02、AC-04）。
6. 失败传播：必需上游失败/被删除/输入不足时，下游阶段置 BLOCKED 并记录原因，不作为正常成功路径启动；无关分支默认继续推进；任务在无活跃阶段且存在失败必需路径时终态 FAILED，否则推进至全部必需出口完成后 SUCCEEDED（RUN-07、D-04）。

### 6.2 状态机（OBS-01）

Task 状态（用户可见）：QUEUED / RUNNING / PAUSING / PAUSED / CANCELLING / CANCELLED / SUCCEEDED / FAILED / BLOCKED / RECONCILING。

TaskStage 状态：WAITING_DEPS / READY / DISPATCHING / RUNNING / AWAITING_APPROVAL / PAUSING / PAUSED / RETRYING / SUCCEEDED / FAILED / SKIPPED / CANCELLED / BLOCKED / LOST / RECONCILING。

规则：

- desired/observed 双轨；控制操作（暂停/恢复/删除/调序）只写 desired_state 并 bump control_epoch；执行器收敛后写 observed_state。「控制操作尚在处理中」必须可见（OBS-01、AC-22）。
- 迁移以 (entity_id, control_epoch) CAS 提交；完成回调携带 lease_id 与 generation，租约已吊销或代次过期的回调直接丢弃（REC-05、AC-11）。
- LOST 表示系统无法确认真实状态（如崩溃后对账失败），是显式状态而非伪装成 RUNNING（OBS-01、REC-04）。
- 同一节点一次只主动执行一个阶段（节点串行约束，R §5.2.2）；不同任务使用独立 session；暂停、审批等待是否占用执行位置按 D-03 定案（不占执行槽、保留队首，见 14.3）。

### 6.3 调度循环（伪代码）

```pseudo
loop:
    reconcile_once_if_booting()            # 见第 11 章
    for stage in stages where observed=READY
                   and all_required_upstreams_succeeded(stage, stage.task_id)
                   and node_slot_free(stage.node_id)          # 节点串行
                   and workflow_capacity_ok(stage.workflow_id):
        claim(stage, expected_epoch = stage.control_epoch)    # CAS；失败则跳过
        dispatch(stage) -> new Attempt                        # 分配 session、登记资源
    sleep(poll_interval)                                       # 事件驱动优先，轮询兜底
```

### 6.4 节点级排序（RUN-04）

调序请求 = 对目标节点的 READY/QUEUED 阶段集合重排 node_priority。与派发并发时，CAS 失败的一方收到「实际生效结果」而非静默覆盖（AC-03）。暂停、已删除、失败、完成阶段不参与调序。

---

## 7. 跨节点交接与上下文（DATA-01–06、CFG-04–06）

本章是对原 Proposal Concern 3 七问的正式回答，同时落实 R §2.1.4 的澄清：摘要生成、prompt 注入、路径托管是框架自身的机制，对用户透明；AI 建图只负责产出定义图，不参与运行时交接。

### 7.1 传输模型

- 阶段完成时发出 data_ready 消息，负载只携带产物引用与元数据（producer 三元组、kind、sensitivity）；正文不随通知传输。
- 下游节点通过系统提供的只读读取工具按需拉取产物全文；小负载（建议阈值 32KB，可调）可内联，内联与引用共用同一份校验路径。
- 产物不可变；「修改」一律表达为派生新版本（版本 +1，记录 lineage）。已完成消费者的输入来源保持钉扎，不被改写为「最新结果」（DATA-02/04）。
- 共享产物（fan-out）读取同一份不可变副本，无写冲突；共享文件工作区的隔离方案见 14.6（D-06）。

### 7.2 摘要与交接失败

- 上游产出 Artifact 时由 Summarizer 同步生成摘要；摘要须覆盖该边声明的输出契约要点，覆盖不足即交接失败并显式报出，不允许以静默省略换取「成功」（DATA-03）。
- 引用失效、访问被拒、必需文件缺失同属交接失败，下游阶段置 BLOCKED 并给出可定位原因（AC-20）。

### 7.3 ContextPackage 组装（CFG-06、DATA-05）

下游 Attempt 启动前，组装器生成唯一的数据源注入 system prompt 与会话输入：

| 分区 | 内容 | 来源 | 默认预算占比* |
| --- | --- | --- | --- |
| P1 角色与任务 | 节点 role、本次任务说明、发射输入 | 节点定义 + Task.input_payload | 5% |
| P2 输入与上游材料 | 每条有效上游边一节：摘要 + 产物引用 + 访问方式 | Edge 契约 + Artifact | 25%，超支按「全文→短摘要→纯指针」降级并记录 |
| P3 输出要求 | 本节点输出契约与格式示例 | Edge 契约 | 10% |
| P4 工具与权限 | Skills 结构化摘要、MCP 工具说明、审批策略、禁止操作 | skill_refs / tool_refs / 适配器 | 10% |
| P5 运行保留 | 会话内推理与工具结果 | — | 50% |

\* 占比按当前生效候选的实际上下窗口折算为 token；窗口见 7.4。

约束：system_prompt 留空时仍能以 P1/P2 执行；填写 system_prompt 不取消必需交接；上游材料只作为数据进入 P2，不获得修改系统约束的权限（prompt 注入隔离）；不混入其他任务的私有历史与凭据（DATA-05）；组装记录写入事件日志，用户可查看交接内容与来源。

### 7.4 Compact 机制（CFG-04/05，落实 R §1.3.2 澄清）

- 语义：用户填写的 compact_threshold 是**期望触发整理的阈值**，实际触发点为 min(用户阈值, harness 实际上下文上限) − 安全余量（余量建议 10%，供整理输出本身使用，D-05）。
- harness 实际上限未知或用量无法测量时：以用户阈值为准并在 UI 标注「上限未验证」；二者皆无则该节点声明「不支持自动整理」，不得以静默截断冒充整理。
- 整理由适配器执行（能力声明的一部分）；整理前后保留继续任务所需的信息（任务目标、输出契约、未完成事项），整理事件写入 Attempt.compact_events；整理失败按明错误处理——阻塞并提示用户，或按用户预设切换到更大窗口的候选。

### 7.5 反馈边界（DATA-06）

第一版的反馈最低要求：下游发现输入不足或需返工时，生成 FEEDBACK 消息，结构化携带问题描述与对应上游产物引用，**呈现给用户**（OBS-05 入口），不自动唤醒已完成上游。Message 信封与 Attempt 中预留 rework_round 字段；自动返工（有界循环、失效传播）按 D-06 定案为首版不实现，字段预留使二期无需改数据模型（见 14.6）。

---

## 8. Harness 适配层（HAR-01–03、D-09）

### 8.1 适配器六组契约

将原 Proposal「应该以明确的形式规定下来」落实为接口族。每个适配器实现以下六组契约，能力缺失必须在 manifest 中如实声明：

| 契约组 | 操作 | 说明 |
| --- | --- | --- |
| capabilities | declare(), probe() | 声明 + 实测探测；能力声明是配置兼容性与 UI 提示的依据（HAR-02） |
| session lifecycle | create / resume / dispose / list | 创建、恢复、销毁、枚举会话；指令差异（如 /sessions vs /resume）在适配器内部消化，不上泄 |
| io | send_input / read_output / stream_events | 输入注入与输出读取；输出解析为结构化事件流 |
| permission hook | on_permission_request -> Approval / respond(decision) | 把 harness 的权限确认事件转译为 Approval 实体并回注决定；不支持该钩子的适配器不得声称支持非自动权限模式（HUM-03） |
| event stream | subscribe() | 状态变化、后台工作、compact、错误事件 |
| health / version | heartbeat / handshake / compat_check | 心跳租约、协议握手、版本兼容定位（HAR-03） |

### 8.2 Session 模型（落实 R §5.2.2 澄清）

- 节点逻辑上是**一组 session**：同一节点串行处理不同阶段，每执行完一个阶段，该 session 归档并可为下一阶段新开 session；节点因此关联多个历史 session。
- 恢复同一任务的同一阶段时，若 harness 支持 resume 且 session 仍在 supervisor 台账中，优先复用原 session；否则新开 session 并以 checkpoint + 摘要重建上下文。两种路径都必须在历史中留痕（OBS-02/03）。
- 「不同任务不共享上下文」由「每个 Attempt 独占 session」保证。

### 8.3 一致性与降级（HAR-03）

相同用户操作（暂停、打断、删除）在不同 harness 下必须有相同的产品语义；底层做不到时返回可理解原因或明确替代（如「该 harness 不支持原位暂停，将停至当前安全点并重建」），不得永远显示 running。适配器或 harness 版本升级导致的不兼容通过 compat_check 在配置期与启动期两处定位。

---

## 9. 安全与治理（AUTH-02、HUM-03–05、AI-01/02、UI-02）

### 9.1 凭据治理

- 凭据本体只存于 Secret Store（建议实现见第 15 章）；其余一切位置（节点、模板、事件、日志、摘要、Graph Capture 材料）只出现 credential_ref。
- 模板序列化强制剥离凭据为占位引用，实例化时引导重绑定（TPL-03、AC-09）。
- 共享凭据撤销：影响面展示（引用它的 Workflow/节点/在途任务清单）；在途 Attempt 允许完成当前请求，新 Attempt 必须重新绑定有效凭据，不得以钉扎快照绕过已撤销的访问权限（CFG-07、D-02）。
- 事件与历史落库前经脱敏过滤器；展示层对凭据字段只显示 label 与状态。

### 9.2 审批闭环

- 拦截点统一在适配层：permission hook 把各 harness 的权限事件转译为 Approval；等待期间受保护操作不得执行（HUM-03）。
- 通知经 WebSocket 推送到客户端「需处理」入口（OBS-05）；客户端断连期间审批保持 pending，重连后找回（AC-12）；超时默认 deny_pause（14.9 定案）。
- 决定回注：approve / deny / 修改后 approve 作为结构化结果回注原会话；回传失败标记 undeliverable 并可追溯（HUM-04）。
- 失效规则：任务删除、尝试失效、命令内容变化均使旧批准作废；重复通知不重复授权（AC-14）。

### 9.3 AI 写操作闸门（HUM-05、AI-01/02、D-12）

- AI 建图与 Graph Capture 的产物一律为草稿：过同一校验管线、diff 预览、用户显式采用后才成为 revision；AI 不得编造凭据、工具或不存在的本机 harness，缺失项标「待配置」（WF-02/03、AC-16）。
- AI 对既有流程/工具/权限的修改建议同样走 diff + 显式确认；secret 绑定类字段禁止 AI 填新值，只允许引用既有凭据。
- Workflow 作为 Base Assistant（AI-02）：保留该能力；启动前检查助手引用图（assistant → workflow 依赖关系）无直接及间接自环；被引用的 Workflow 必须处于可用状态，其失败向助手任务显式传播，不得递归自举（D-12 定案见 14.12）。

### 9.4 出站与边界

- Web 服务默认绑 loopback + 访问令牌；开放局域网访问需显式配置并显示警告（UI-02）。
- Artifact 携带 sensitivity 并沿血缘取最高级；向不同 harness/厂商边界传递前经出站扫描与脱敏，凭据任何情况下不出系统。
- 风险分级与审批策略挂在工具级（ToolSpec.risk_level / approval_policy）；本文不引入覆盖所有危险命令的独立风险分类器（遵从清单 §3.8 注记）。

---

## 10. 生命周期控制与资源治理（LIFE-01–06、RES-01–03）

### 10.1 操作范围（落实清单 §3.9 收敛）

暂停与删除默认作用于**整次任务**，覆盖其并行分支；从某节点发起时，节点是定位任务与记录操作来源的入口（origin_of_control 字段）。第一版不提供分支级单独取消；保留扩展位但不得与整任务操作共用文案。

### 10.2 暂停与恢复（LIFE-01/02/03、D-07）

```pseudo
pause(task, origin):
    task.desired_state = PAUSED; task.control_epoch += 1        # CAS 写入
    for stage in task.stages where observed in {READY}:         # 停止新派发
        stage.observed = PAUSED                                 # 未启动，零成本
    for stage in task.stages where observed in {DISPATCHING, RUNNING, RETRYING, AWAITING_APPROVAL}:
        stage.observed = PAUSING
        adapter = adapters[stage.current_attempt.profile.harness_ref]
        if adapter.capabilities.pause_in_place:
            adapter.pause(session_ref)                          # 原位暂停
        else:
            checkpoint = adapter.checkpoint(session_ref) 或 None
            cancel_chain(stage, keep_checkpoint=checkpoint)     # 协作停止，见 10.4
        stage.observed = PAUSED  # 仅当确认停止后
    # 全程过渡态可见；无法原位暂停时如实说明替代路径与可能重复的工作（LIFE-02）
```

恢复：PAUSED 任务从保留状态继续——未启动阶段直接重入就绪集；被协作停止的阶段创建新 Attempt，输入 = 钉扎快照 + 已确认上游产物 + checkpoint（如有）；不重跑已成功阶段，不改写历史（LIFE-03）。用户主动暂停的意图持久化，系统重启后不得自动恢复运行（REC-03、清单 §2）。

### 10.3 主动删除（LIFE-04/05/06）

- 删除任务：desired_state = CANCELLED；停止所有排队、重试与执行（cancel_chain，不保留 checkpoint）；资源台账遍历清理；已完成阶段以其真实结果留在历史，不改写为失败；任务进入 CANCELLED 终态后无续跑入口，迟到输出按 generation 丢弃（REC-05）。
- 删除 Workflow：停止接受新提交 → 终止其全部未结束任务（逐任务走删除路径）→ 清理归属资源 → 状态置 deleted，从可用列表移除；定义与任务历史保留供回看；不删除仍被其他 Workflow 引用的共享配置（LIFE-05）。
- 完成判据三态分离（LIFE-06）：「已接受删除」（desired 写入成功）、「执行已停止」（全部 Attempt 确认终止）、「资源清理完成」（台账句柄全部 closed）。清理失败或远端工作无法确认的对象保持可见（teardown_failed / LOST），提供继续处理入口，禁止直接报告全部完成。删除只终止后续执行，不承诺撤销已发生的文件写入或外部副作用。

### 10.4 统一取消协议（cancel_chain）

所有「停止」语义（暂停的非原位路径、删除、停用撤回、Workflow 删除、崩溃清理）复用同一条取消链：

```pseudo
cancel_chain(stage, keep_checkpoint=None):
    attempt = stage.current_attempt
    adapter.abort_stream(attempt.session_ref)        # 先停远端流，终止继续计费
    adapter.terminate(attempt.session_ref, SIGTERM)  # 软终止，给落盘宽限期
    wait(grace_period)
    if still_alive(attempt): adapter.terminate(..., SIGKILL)   # 超时升级
    for res in resource_ledger.where(owner=attempt):           # 只信台账
        res.teardown()  # 失败 → teardown_failed，移交周期清理器并可见
    if keep_checkpoint: persist(keep_checkpoint)
    attempt.generation += 1                          # 迟到回调按代次丢弃
```

### 10.5 周期清理器（Reaper）

周期运行 + 服务启动时运行，职责三项：(a) 台账与实际进程表对账，回收孤儿进程、端口、连接（RES-02）；(b) 处理 teardown_failed 句柄的重试与告警；(c) 超龄 tombstone 产物在「无活跃任务或可恢复任务引用」检查后物理回收（RES-03；历史数据本身不强制自动 TTL，提供存储占用查看与手动清理入口，遵从清单 §4 对「历史≠泄漏」的澄清）。清理器只清理能确认归属的对象；无法确认归属时报告而非隐藏。

---

## 11. 异常恢复（REC-01–05、D-08）

### 11.1 三类故障的分工

| 故障 | 机制 |
| --- | --- |
| 客户端断连 | 任务由 supervisor + core 托管，与浏览器连接无关；重连后拉取同一任务的真实状态与待处理请求（REC-01、AC-12） |
| 节点失效（模型连接/harness/session 异常） | 适配器上报错误 → 按 CFG-03 退避重试/切换候选；harness 崩溃时 supervisor 发现心跳缺失，尝试 resume session 或从最近确认点重建（REC-02）；恢复失败有终止条件并展示原因与重试/终止/核对入口（REC-04） |
| Workerbee 自身重启 | 启动对账（11.2） |

### 11.2 启动对账（startup reconcile）

```pseudo
on_core_restart():
    for stage in stages where observed not in TERMINAL:
        stage.observed = RECONCILING               # 先全部置核对中，仅接受用户控制
    for stage in reconciling:
        attempt = stage.current_attempt
        if supervisor.session_alive(attempt.session_ref) and lease_fresh(attempt):
            reattach(stage)                        # 恢复监控，不重启工作
        elif attempt has completion_marker and artifact_verified(stage):
            confirm_success(stage); advance_downstream(stage)   # 按 dedup_key 去重
        elif checkpoint_available(attempt):
            mark_resumable(stage)                  # 等待重放决策
        else:
            stage.observed = FAILED(reason=system_restart)  # 或 LOST（无法确认时）
    enforce_user_intents()          # desired=PAUSED 保持暂停；CANCELLED 不复活
```

纪律：恢复 running 记录不等于确认进程存活，必须以 supervisor 台账核对为准（REC-03）；恢复审批记录不等于恢复旧授权有效性（HUM-04）；同一完成通知重复到达按 dedup_key 幂等，不重复启动下游、不覆盖较新的已确认结果（REC-05）；无法判断外部副作用是否完成时进入 LOST/待核对，不盲目重放（REC-04）。本设计不承诺任意外部工具「恰好一次」（清单 §2）。

### 11.3 断点续跑（partial resume）

任务在 N1✓→N2✓→N3✗ 中断后，从 N3 新建 Attempt 重放：输入 = N2 产物引用（内容寻址、不可变）+ 发射时钉扎的配置快照；新 Attempt 继承血缘，历史不污染。主动删除的任务无此入口（清单 §2 与 §4 的区分在此落地：恢复只针对故障，不针对用户主动删除）。

---

## 12. 模块与文件拆分

建议 monorepo 布局（以第 15 章技术栈为例；目录即模块边界，跨目录只允许经公开接口导入）。后端、内核、supervisor 与适配器全部为 Python 包；仅 `web/` 为 TypeScript：

```text
workerbee/
├── pyproject.toml                   # 单仓单包，以 namespace 子包划分模块边界
├── src/workerbee/
│   ├── core/                        # L1 + L2：纯领域逻辑，禁止直接 IO
│   │   ├── domain/                  # 实体与值对象（dataclass / pydantic 模型）
│   │   │   ├── workflow.py          # WorkflowDefinition, WorkflowRevision
│   │   │   ├── node.py              # NodeDefinition, ExecutionProfile
│   │   │   ├── edge.py              # Edge, EdgeContract
│   │   │   ├── template.py          # Template（含凭据剥离/重绑定）
│   │   │   ├── task.py              # Task, TaskStage, Attempt, 状态机定义
│   │   │   └── registry.py          # SkillDoc, ToolSpec, CredentialRef, HarnessRegistration
│   │   ├── graph/
│   │   │   ├── derive.py            # derive() 有效图派生（纯函数）
│   │   │   └── validate.py          # 校验管线（4.4 全部校验项）
│   │   ├── runtime/
│   │   │   ├── scheduler.py         # 调度循环、claim/CAS、容量背压
│   │   │   ├── lifecycle.py         # 暂停/恢复/删除/停用编排
│   │   │   ├── cancel.py            # cancel_chain 两级取消协议
│   │   │   ├── reconcile.py         # 启动对账与断点续跑
│   │   │   └── queue.py             # 权威队列 + 节点投影 + 调序
│   │   └── resources/
│   │       ├── ledger.py            # 资源台账
│   │       └── reaper.py            # 周期清理器
│   ├── data/                        # L4
│   │   ├── artifact_store.py        # 内容寻址产物存储 + 引用计数
│   │   ├── message_bus.py           # Message 信封、投递、dedup
│   │   ├── event_log.py             # append-only 事件日志
│   │   ├── context_assembler.py     # ContextPackage 组装与预算
│   │   └── summarizer.py            # 摘要生成与质量门禁
│   ├── adapters/                    # L3
│   │   ├── sdk/                     # 适配器 SDK：六组契约类型 + JSON-RPC 通道
│   │   ├── host/                    # Adapter Host：插件加载、隔离、协议协商
│   │   └── (claude_code|kimi_code|…) # 各 harness 适配器实现（独立子包）
│   ├── security/                    # L5
│   │   ├── secret_store.py          # 凭据存取接口与实现
│   │   ├── approval_gateway.py      # 审批生命周期、超时、回注
│   │   ├── ai_gate.py               # AI 草稿 diff 与确认闸门
│   │   └── redaction.py             # 出站脱敏与敏感级传播
│   ├── supervisor/                  # 独立进程入口：session 托管、心跳、租约（PTY 管理）
│   ├── server/                      # API 网关（FastAPI）：REST + WebSocket；认证；revision CAS 端点
│   └── tui/                         # 可选终端客户端（复用 server API，不另造任务事实）
├── web/                             # L6：React + TypeScript 客户端（拓扑编辑/监控/审批/历史/清理入口）
├── docs/                            # 本文档及后续设计文档
└── tests/
    ├── fuzz/                        # 启停 fuzz（AC-05）、随机编辑序列（hypothesis 做基于性质的测试）
    └── scenarios/                   # AC-01–AC-22 场景测试
```

边界规则：`core` 不 import `server`/`adapters` 实现（只依赖接口）；`data` 与 `security` 互不感知对方内部；适配器实现包只能依赖 `adapters/sdk`；`web` 只能经 `server` 公开 API 访问。并发模型：core 内以 asyncio 单事件循环承载调度与 IO，CPU 密集或阻塞操作（产物读写、脱敏扫描）交给线程池；supervisor 为独立 asyncio 进程，与 core 经本地 socket 通信。

---

## 13. 需求映射表

每组功能编号 → 承接的层/模块/关键实体。逐条映射的细表建议在实现期由本表展开维护。

| 需求组 | 承接位置 | 关键实体/机制 |
| --- | --- | --- |
| WF-01–05 建图与校验 | L1 `domain/` + `graph/validate.py` | WorkflowRevision、Edge、校验管线、草稿态 |
| WF-06 运行中编辑 | L1 + L2 | revision 钉扎、生效范围说明、CAS |
| WF-07 多 Workflow | L1 + L6 | WorkflowDefinition、任务隔离（共享配置见 EXT-03） |
| ACT-01–04 启停 | L1 `graph/derive.py` + L2 `runtime/lifecycle.py` | enabled 标志、derive、存量任务处理矩阵（D-01） |
| CFG-01–07 执行配置 | L1 `domain/node.py` + L4 `data/context_assembler.py` | ExecutionProfile、compact 机制、实际配置留痕 |
| HAR-01–03 | L3 `adapters/` | AdapterManifest 六组契约、能力声明与降级 |
| AUTH-01/02 | L1 `domain/registry.py` + L5 `security/secret_store.py` | CredentialRef、引用化、脱敏 |
| EXT-01–03 | L1 `domain/registry.py` + L6 | SkillDoc、ToolSpec、共享配置变更影响面 |
| RUN-01–07 | L2 `runtime/` | Task/TaskStage/Attempt、幂等键、汇聚与失败传播 |
| DATA-01–06 | L4 `data/` | Artifact、Message、ContextPackage、FEEDBACK |
| OBS-01–05 | L2 + L4 + L6 | 双轨状态机、Event Log、usage 字段、需处理入口 |
| HUM-01/02 | L3 + server | btw/打断消息经适配器路由到正确 session（AC-14） |
| HUM-03–05 | L5 `security/` | Approval、AI 写闸门 |
| LIFE-01–06 | L2 `runtime/lifecycle.py` + `runtime/cancel.py` | 整任务操作范围、取消协议、三态完成判据 |
| REC-01–05 | L2 `runtime/reconcile.py` + supervisor | 启动对账、断点续跑、幂等去重 |
| RES-01–04 | L2 `resources/` + L6 | 资源台账、Reaper、历史清理入口、Skill 边界标注 |
| TPL-01–03 | L1 `domain/template.py` | 凭据剥离/重绑定、变更不静默改写 |
| AI-01/02 | L5 `security/ai_gate.py` + L1 | Base Assistant 配置、递归依赖检查 |
| UI-01–03 | L6 + server | Web 主客户端、loopback+令牌、可选 TUI |
| PLAT-01 | supervisor + `adapters/` | 平台矩阵声明（D-13） |
| D-01–D-13 | 第 14 章逐项定案 | — |
| AC-01–AC-22 | `tests/scenarios/` | 验收场景随实现落地 |

---

## 14. D-01–D-13 逐项定案

每项给出「选择／理由／用户影响／验证场景」。标注【建议】的条目表示这是我的设计决策、作者可推翻；推翻时应同步更新清单与验收场景（遵从清单第 7 节维护规则）。

### D-01 启停与存量任务的处理矩阵

- **选择**：停用操作发起时，UI 先展示影响预览（受影响节点、依赖、任务范围，ACT-02），并要求操作者在两种存量处理方式中显式选择：**(a) 排水（默认）**——节点置 draining 态，正在执行的阶段跑完，排队阶段保留，节点不再领取新阶段；全部存量结束后 enabled 翻转、有效图版本递增。**(b) 立即撤回**——对应原 Proposal「撤回已有 request」的意图：在途阶段走 cancel_chain（不保留 checkpoint），排队阶段标记 SKIPPED，enabled 立即翻转。启用操作立即生效：节点恢复参与派生，排水期排队的阶段按新有效图重新评估（若依赖关系已变化则按新依赖执行，并记录）。
- **理由**：排水是安全默认（不丢弃已完成工作）；立即撤回保留原 Proposal 的强制语义但代价显式化；两种路径都满足「已有任务不丢失、不重复推进、不无说明跳过」（ACT-04）。
- **用户影响**：停用从瞬时操作变为「预览 → 选策略 → 观察收敛」三步；排水期节点显示过渡态。
- **验证场景**：AC-05（启停往返一致）、AC-06（入口/出口/输入衔接）、AC-07（运行中改图的生效范围）。

### D-02 配置生效与并发编辑

- **选择**：revision 化 + 乐观并发。一切编辑请求携带 base_revision_seq，服务端 CAS 提交，冲突返回最新版本与 diff，由用户决定合并或重试。任务发射钉扎 revision；运行中编辑只影响新任务与未启动阶段的下次 Attempt（在途 Attempt 继续按钉扎配置）。共享凭据/工具/Skill 的修改与撤销：展示受影响节点与在途任务；在途 Attempt 完成当前请求，新 Attempt 必须使用新值；已撤销凭据不得被钉扎快照绕过。
- **理由**：乐观并发对「单用户多标签页」场景足够，避免编辑锁的可用性代价；「在途完成当前请求」在安全与可用性间的取舍——已建立的认证连接立即掐断的代价高于收益。
- **用户影响**：冲突时看到差异并显式选择；共享配置变更前有明确影响面提示。
- **验证场景**：AC-07、AC-11、AC-19。

### D-03 调度位置与有限资源

- **选择【建议】**：节点执行槽 = 同一节点同一时刻至多一个 RUNNING/DISPATCHING 阶段。PAUSED、AWAITING_APPROVAL、RETRYING（退避中）、RECONCILING 阶段**不占执行槽**，释放 session 与 stream 资源；恢复时按 queue_order 重新竞争槽位，恢复中的阶段在队列中标注「恢复中」而非普通 pending。队列默认顺序 (node_priority, task_priority, enqueued_at)；无效调序返回原因；容量不足（workflow 级并发上限或全局背压）时新阶段保持 READY 排队并在 UI 说明原因。
- **理由**：审批与退避占槽会把安全问题传导为资源占用（一个无人值守的审批请求锁死整条流水线）；释放槽位牺牲严格的队首阻塞，换来吞吐与可恢复性。
- **用户影响**：审批等待期间同节点后续任务可继续执行；用户需要理解「等待中不占位」的语义（UI 明确标注）。
- **验证场景**：AC-03、AC-12、AC-22。

### D-04 失败、完成与控制范围

- **选择**：阶段成功 = 会话正常结束 ∧ 要求产出通过边契约校验 ∧ 无未结束的结果性后台工作。必需上游失败 → 下游 BLOCKED（记录原因），不启动；无关分支继续；任务在无活跃阶段且存在失败必需路径时终态 FAILED，否则推进至全部必需出口完成后 SUCCEEDED。暂停/删除默认整任务范围（清单 §3.9），不提供分支级操作。
- **理由**：「全部必需上游成功」是清单已定的首版汇聚语义；整任务控制消除「任务链」歧义。
- **用户影响**：失败任务的界面必须同时显示失败原因与尚在运行的分支（RUN-07）。
- **验证场景**：AC-04、AC-10、AC-22。

### D-05 候选与上下文

- **选择【建议】**：错误分类——网络错误、限流（429）、5xx 为可重试；认证失败、4xx 配置错误、契约校验失败为不可重试（直接切换候选或失败）；退避 = min(base × 2^n, cap) + 抖动，默认 base=1s、cap=60s、max_attempts 由用户在候选上配置（默认 3）。候选切换严格按优先级单向推进，已失败的候选在同一次阶段执行中不再回访（杜绝 A↔B 震荡）；全部候选耗尽 → 阶段 FAILED，展示各次原因与可选后续动作。compact 安全余量默认 10%；更小上下文候选无法承接当前 ContextPackage 时，按 7.3 降级链压缩 P2，仍不足则该候选判不可重试失败并切换。
- **理由**：单向推进以「不回头」换取终止性证明的平凡化；错误分类覆盖主流 API 形态，适配器可覆盖声明。
- **用户影响**：失败时看到的是带原因列表的终态，而非无声卡住。
- **验证场景**：AC-08。

### D-06 交接与反馈

- **选择**：摘要质量门禁 = 摘要须覆盖边输出契约的必填要点，不足即交接失败。产物版本选择 = 下游在就绪判定时钉扎各有效上游「该任务内当前成功」的产物版本，之后不变。共享文件工作区【建议】：每个 Attempt 独立工作目录，跨阶段共享经 Artifact Store 的只读副本 + 派生写回；同一任务内确需共享工作目录的节点可显式配置共享卷并以节点串行约束规避并发写。自动返工：**首版不实现**，FEEDBACK 只呈现给用户；rework_round 等字段预留。
- **理由**：自动返工的失效传播（哪些下游结果作废）在首版复杂度下风险大于收益；字段预留保证二期平滑。
- **用户影响**：下游报告输入不足时，用户手动定位上游结果并决定重跑或调整（AC-20）。
- **验证场景**：AC-20、AC-04（共享产物）。

### D-07 暂停与恢复能力

- **选择**：能力矩阵逐 harness 声明四档：原位暂停 / 协作停止 + checkpoint 重建 / 协作停止 + 从头重跑本阶段 / 不支持（拒绝暂停请求并说明）。默认路径为「协作停止 + 尽量 checkpoint」，恢复时新 Attempt 继承血缘。异常后的可用恢复点 = 最近一个已确认成功的上游产物 + 该阶段的 checkpoint（如有）。
- **理由**：多数终端 harness 不支持可靠原位暂停，把「暂停」统一承诺为无损冻结不可兑现；分档如实呈现差异（LIFE-02）。
- **用户影响**：不同 harness 下暂停的代价不同，UI 在操作前显示预期代价。
- **验证场景**：AC-10、AC-13。

### D-08 持久化与故障核对

- **选择【建议】**：状态存储 = 关系表（当前态）+ append-only 事件日志（迁移与操作留痕），控制操作先写事件日志再改状态（写前日志）；session 实况以 supervisor 台账为准，「数据库有记录」不等于「进程存活」；重复消息按 dedup_key 幂等；外部副作用不确定 → LOST + 人工核对入口；孤儿执行由 Reaper 对账回收。
- **理由**：事件日志同时服务监控、审计与对账三个需求，一处投入三处收益。
- **用户影响**：重启后短暂出现「核对中」状态，属正常。
- **验证场景**：AC-13、AC-15。

### D-09 Harness 接口与审批

- **选择**：即 8.1 六组契约 + 8.3 降级规则；适配器协议版本化（protocol_version 握手，不兼容给出明确错误而非挂起）；审批往返经 permission hook 转译为 Approval，超时默认 deny_pause；无法适配的功能（如某 harness 无审批钩子）在 manifest 如实声明，配置期与运行前提示。
- **理由**：契约是插件生态的地基；「不能声称支持」比「声称但卡死」重要。
- **用户影响**：配置 harness 时即可见能力清单与限制（HAR-02）。
- **验证场景**：AC-01、AC-14、AC-18。

### D-10 资源约束责任

- **选择**：首版框架只保证**资源归属与释放义务**（RES-01/02，台账 + Reaper），不提供框架级硬配额；Skill 中的资源约束声明为执行指导，UI 明确标注「由模型自觉遵守，框架未强制」（RES-04、AC-22）；ResourceRecord 预留 quota 字段，二期可选接入 cgroup/作业对象做硬限制，届时提供「从可识别项选择 + 填关键数值」的简化配置（R §3.1.4 的方向）。
- **理由**：尊重作者对复杂度的保留意见；硬配额的平台差异（Linux cgroup v2 / macOS 缺失 / Windows Job Object）首版消化成本高。
- **用户影响**：资源超限不会被框架阻止，只会在事后可见。
- **验证场景**：AC-15、AC-22。

### D-11 数据与安全边界

- **选择**：凭据存 Secret Store（建议实现见第 15 章），其余位置只存引用；临时文件归属到 Attempt，随台账清理；可恢复进度（checkpoint）与历史分开存储，历史清理入口须拒绝清理仍被活跃或可恢复任务引用的数据；Web 默认 loopback + 令牌；共享资源的最后使用者退出时负责清理，清理失败移交 Reaper 并可见。
- **验证场景**：AC-15、AC-17、AC-19、AC-21。

### D-12 助手与捕获

- **选择**：Graph Capture 只使用可实际获取的资料（显式 plan、输出、工具调用记录、产物），生成的草稿中区分「观察到的依赖」与「推断的依赖」并分别标注；模板生成为独立操作，不重新执行原任务（WF-03）。Workflow 作为 Base Assistant：启动前提 = 被引用 Workflow 已发布且可用；对 assistant→workflow 引用关系做**直接与间接**环检查（传递闭包），成环拒绝配置；助手执行失败显式传播给调用方，不递归自举。
- **验证场景**：AC-16、AC-21。

### D-13 平台与交付批次【建议，待作者确认】

- **选择**：目标平台 Windows / Linux / macOS（PLAT-01）；首版一等支持 Linux 与 macOS，Windows 标注限制项（信号语义差异用 Job Object 抽象、无原生 SIGTERM 时取消链退化为 TerminateProcess + 宽限约定）；不依赖 tmux。交付批次建议：M1 手动建图闭环 + 两个真实 harness 适配器（清单 §1.2 要求核心闭环验收覆盖至少两种 harness 交接）；M2 恢复/审批/资源治理加固；M3 AI 建图、Capture、模板；M4 终端客户端与平台扩展。批次仅为建议，不构成已批准排期。
- **验证场景**：AC-18；M1 出口 = AC-01/02/04/05 通过。

---

## 15. 建议技术栈

以下为设计建议，不是功能要求；下一任实现者可在不改变机制语义的前提下替换。

| 部分 | 建议 | 理由 |
| --- | --- | --- |
| 语言 | Python ≥ 3.12（内核、server、supervisor、适配器、TUI）；TypeScript 仅用于 Web 前端 | 作者偏好 Python 优先；asyncio 足以承载单机调度与 IO 并发；PTY/子进程管理有成熟方案（`pexpect`/`ptyprocess`，Windows 用 `pywinpty`）；MCP 官方提供 Python SDK |
| 状态存储 | SQLite（WAL 模式），经 `sqlite3`/`aiosqlite` 访问 | 单机部署、零运维、事务满足 CAS 与写前日志；事件日志与状态表同库 |
| 数据模型与校验 | pydantic（实体 schema、EdgeContract、AdapterManifest 校验） | 类型即文档，校验管线直接复用 |
| 产物存储 | 文件系统内容寻址目录（digest 命名）+ SQLite 元数据 | 不可变、去重、易清理 |
| 适配器插件 | 子进程 + JSON-RPC over stdio | 崩溃隔离、语言中立（第三方可用任意语言写适配器） |
| 服务端 | FastAPI（REST + WebSocket） | 与 pydantic 同源；推送通道承载监控事件与审批通知（OBS-05） |
| 前端 | React + React Flow（拓扑编辑） | 浏览器客户端无可替代；拓扑图编辑是核心交互，React Flow 生态成熟 |
| TUI（可选） | Textual | 与 server 复用同一 API，Python 内聚 |
| Secret Store | 首版：AES-GCM 加密文件 + 用户口令派生密钥（`cryptography`，Argon2）；二期：OS keychain（`keyring` 库统一 Keychain / libsecret / DPAPI） | 首版跨平台一致性优先；keychain 各平台差异纳入 PLAT-01 矩阵 |
| 分发形态 | 本地服务 + 浏览器打开（首版），`uv`/pip 安装；二期可选 PyInstaller 单文件或桌面包壳 | 避免过早承担桌面壳层复杂度 |
| supervisor 守护 | systemd user service / launchd / Windows Service | 由安装器按平台注册 |
| 测试 | pytest + hypothesis（基于性质的 fuzz，对应 AC-05/AC-11） | 性质测试直接承载「启停后恒为 DAG」「CAS 竞态」类不变量 |

已考虑并放弃的备选：TypeScript 全栈（前后端单语言、MCP SDK 最成熟，但与作者的 Python 偏好冲突；若日后前端团队扩大可重估，机制设计不变）；独立消息队列（单机规模不需要，Message Bus 用 SQLite 表 + asyncio 通知即可）；tmux 托管（R 已澄清仅是举例，且 Windows 无原生等价物）。

Python 化带来的两点代价，如实声明：(a) Web 前端仍是 TypeScript，仓库为双语言，接口契约（REST/WebSocket schema）需由 pydantic 模型生成 OpenAPI 后供前端消费，避免两处手写漂移；(b) asyncio 生态下三方适配器插件若以子进程隔离（本设计已如此），Python 版本差异不影响内核，插件作者自选环境。

---

## 16. Concern 声明：可能遗漏、错误与给下一任的提醒

以诚实披露为原则，按风险排序。以下每一项都可能包含错误或遗漏，请下一任实现者优先复核。

1. **「数据衔接校验」的机器可判定性有限（4.3）。** 边契约是可选的；未声明契约的边在停用节点后无法机器判断「A 的原始输出能否满足 C」。我的处理是回退为文本交接 + UI 提示，但这意味着 ACT-03 的保障强度依赖用户填写契约的意愿。下一任应评估：是否强制要求多入边节点声明契约。
2. **derive() 的「第一层 enabled 祖先」语义在复杂停用模式下可能不符合直觉。** 例如交错停用多条并行分支时，有效边可能把语义上不该直接相连的节点连起来。无环性可证明，语义合理性不可证明——fuzz 测试只能守住前者。建议补一组「语义意外」人工评审用例。
3. **释放执行槽的调度决策（D-03）与「节点串行」的直觉存在张力。** 暂停阶段不占槽意味着同节点后续任务先行执行，恢复时重新排队；用户若期待「暂停的任务恢复后立即继续」会感到意外。该取舍影响 AC-03/AC-22 的期望描述，需作者确认。
4. **审批超时 deny_pause 与 REC-01 的交互。** 客户端断连期间审批超时拒绝，用户可能认为「我没看到就被拒了」。当前设计选择安全优先；若产品希望断连期间冻结计时器，需要额外机制（本设计未含）。
5. **ContextPackage 预算占比（7.3）是拍脑袋的起始值**，未经实测。token 估算的准确性（不同 harness 的 tokenizer 差异）可能使 compact 触发点系统性偏移，建议实现期先做计量再定默认值。
6. **摘要质量门禁（D-06）依赖 LLM 生成摘要的稳定性**，「覆盖契约要点」的校验本身可能也需要 LLM 判断，存在误判面。这是诚实的能力边界，首版接受。
7. **共享文件工作区（D-06）建议方案的验证不足。** 「独立工作目录 + 产物存储中转」对「多个节点在同一仓库上连续改代码」这一主用例可能过于笨重（每阶段一次导出/导入）。这是本设计中我最不确定的一处，建议下一任用真实编码任务做原型验证后再定稿。
8. **取消协议在 Windows 的退化路径（D-13）只写了方向**，Job Object、控制台进程组信号的具体行为未验证；远端 API stream 的 abort 能否真正停止计费取决于厂商实现，UI 需如实标注「已请求取消，计费以厂商为准」（清单 1.3 已有此边界）。
9. **事件日志的体积增长无上限设计**（清单明确不把自动 TTL 列为必要功能）；但事件日志是高频写入，建议至少实现手动清理 + 按任务归档，避免一年后数据库失控。
10. **多 Workflow 并发时的全局资源背压只给了 workflow 级上限**，没有全局公平调度；两个 Workflow 同时压满本地资源时的行为是「各自排队」，未做配额仲裁。清单未要求，但请在 M2 重新评估。
11. **AI 闸门的 diff 预览对「AI 生成的 Skills 文本」防护有限**——diff 能展示变更，不能证明文本中无注入指令。出站扫描（9.4）缓解但不消除。
12. **本文未经任何形式的实现验证。** 状态机迁移表、CAS 协议、对账流程均可能有逻辑漏洞；请实现方为每个 AC 场景建立可运行测试后再宣称完成，并把 fuzz（启停随机序列）与并发竞态（控制操作 vs 完成回调）作为 CI 常驻项。

---

## 17. 交接说明

- 本文档与《Workerbee 功能需求清单 v0.02》配套使用：清单回答「做什么」，本文回答「怎么组织」。两者冲突时以清单为准，并请在本文修订记录中登记。
- 编号映射（第 13 章）与 D 项定案（第 14 章）是实现期的核对清单；实现中对 D 项的任何偏离都应回写本文并通知作者。
- 建议的下一步：按 M1 批次做技术验证原型（derive + 调度循环 + 一个真实适配器 + 取消链），用 AC-01/04/05 三个场景检验本文假设，再展开全量实现。

*文档版本：Workerbee 架构设计 v0.02.1；整理日期：2026-09-27；作者角色：设计层架构；状态：交接稿，待实现方复核第 16 章 Concern。修订记录：v0.02.1 按作者要求将技术栈调整为 Python 优先（第 12、13、15 章），机制设计与数据模型不变。*