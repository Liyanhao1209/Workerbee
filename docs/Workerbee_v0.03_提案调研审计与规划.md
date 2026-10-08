# Workerbee v0.03 调研审计与实现规划（交付实现版）

- 版本：v0.03-audit-plan-02
- 日期：2026-10-08
- 用途：v0.03 四项功能（Workspace 划分、单命令启动、Web Chat + 文件系统操作、Session Fork 树）的调研审计结论 + 已定案设计 + 实现规划。**本文档是实现的直接输入**，所有设计决策已定案（§2 决策总表），实现者不需再向上请示路线选择；遇到本文档未覆盖的细节，按 §0.4 的红线与范式自行决断。
- 调研基线：代码现状（src/workerbee + web/）、《架构设计 v0.02》、《功能需求清单 v0.02》、v0.01 审计（A）与作者澄清（R）、.handover/ 交接记录。

---

## 0. 口径、红线与既定范式

### 0.1 文档优先级
作者澄清（R）＞ 功能需求清单 v0.02 ＞ v0.01 审计（A）。A 中多条结论已被 R 驳回（删除语义、节点队列归属、session 模型、context_len、能力标签/预算），只能当问题线索。

### 0.2 以代码现状为准
README 与述职报告的缺口清单已过时：Base Assistant、AI 建图、Graph Capture、前端 Vitest 测试均已在 2026-09-29 之后的 commits 落地。本报告所有"现状"结论核实自代码。

### 0.3 审计标尺
沿用 v0.01 → v0.02 的核心教训——"承诺—机制断裂"：每个新功能必须能回答状态机落点、资源归属、失败路径、清理责任。实现期最危险的模式是"静默失效"（两头各测、中间断线），**所有新接线必须有 AC 级端到端测试**。

### 0.4 设计红线（不得破坏）
1. 有效图纯函数派生不持久化；发射即钉扎（revision 快照）。
2. 凭据不出 L5 安全层：密钥本体只进 `security/secret_store.py` 的 vault，其他位置只存 `secret_locator` 引用；所有出站文本过 `SecretRedactor`。
3. 先登记后使用、清理只信资源台账（`managed_roots` 边界）。
4. 失败显式化、禁止静默降级（降级必须可见，如 LLMRouter 的 `degraded_reasons` 先例）。
5. supervisor 不含领域逻辑；三进程职责分离是对 v0.01"自指性缺陷"的回应。
6. 迁移纪律：`data/schema.py` 的 MIGRATIONS 只追加不改写；能用 `meta_kv` 免迁移的就不加表。
7. 前端纪律："WS 推送只是加速器、REST 是事实源"（chunk 累积 + 对账重拉）；写操作带幂等/CAS（参照 `assistant_draft`/`capture_draft` 范式）。

### 0.5 新服务组装范本
新增内核服务照 `capture/` 模式接线：`app.py` 组合根注入 hooks，复用 `LLMRouter`/凭据/脱敏，不 import server 层（`capture/service.py:147`、`app.py:607-616`）。

---

## 1. 现状架构摘要（实现者必读）

### 1.1 进程与启动

```
workerbee-core = Engine（调度/状态机/事件日志/审批/Reaper/助手/捕获）
               + FastAPI 网关（REST + WS，默认 127.0.0.1:8765）
               + StaticFiles 托管 web/dist（app.py:177-184，dist 缺失时给解释页）
  harness 托管二选一：
    ├─ 默认 in_process：HarnessRouter 在 core 进程内拉起适配器子进程
    │   （adapters/host/router.py:302；core 重启 → 在跑任务断）
    └─ --use-supervisor：core 经 Unix socket 连 workerbee-supervisor
        （supervisor/server.py 持有 harness 子进程；core 重启会话不死；
         socket 认证 = 文件权限 chmod 600；core 连不上时降级为
         _UnavailableHarness 不拒绝启动，app.py:227-255）
workerbee（cli.py，Typer）= 客户端 + core/supervisor 薄包装
```

关键事实：
- server 与 web **本来就同进程**，"拉起 server+web"零成本。
- `Engine.create()`/`Engine.stop()` 明确支持嵌入式（app.py:180-181, 810-815）；优雅关闭链路完整（server/main.py:180-188）。
- 数据目录默认**相对路径** `./.workerbee`（cli.py:93、server/main.py:36、app.py:75）。
- **web/dist 路径按源码布局硬算**：`Path(__file__).resolve().parents[3]/"web"/"dist"`（app.py:33），pip 安装后必然失效。
- 认证：单用户单 token（`X-Workerbee-Token` 头或 `?token=`，server/auth.py），自动生成随机 token 只在启动横幅打印一次；默认只接受 loopback；**无端口冲突检测**。
- 现存 bug：`workerbee core --use-supervisor` 声明选项但转发时丢弃（cli.py:217 vs 226-243）；`workerbee supervisor` 包装丢 `--adapter-commands`/socket 路径。
- 配置无 BaseSettings（pydantic-settings 声明了依赖但全库未用），配置来源 = CLI 参数 + 三个环境变量（`WORKERBEE_API`/`WORKERBEE_TOKEN`/`WORKERBEE_PASSPHRASE`）。

### 1.2 数据模型（SCHEMA_VERSION = 11，data/schema.py）

- `workflow` 表**无 workspace/目录列**；全局单一 DB `<data-dir>/workerbee.db`；无租户维度。
- 目录概念全是进程级单例：`EngineConfig.workspace_dir`（app.py:76,122，仅作 `managed_roots` 删除边界，app.py:141-145）、`EngineConfig.node_cwd`（app.py:106，传 Scheduler，app.py:583）、`HarnessRegistration.cwd`。
- 会话类实体三种，**全部线性、无树/分支**：`assistant_thread`/`assistant_message`（append-only，无 parent_id）、`session_handle`（harness 台账，checkpoint/resume ≠ fork）、stage 间 `message`。
- `meta_kv` 表（schema.py:370）：免迁移键值，助手配置已存这里。

### 1.3 Base Assistant（assistant/）—— Chat 功能的最大复用资产

- 链路：线程历史 → 滑窗截断（memory.py:54）→ system prompt（角色 + guidebook + 系统状态快照 + 钉扎摘要）→ `LLMRouter.stream` → WS 推 `assistant_chunk`（text/reasoning 分流，service.py:515-535）→ 脱敏落库 → 推最终消息。
- 凭证自配：`AssistantConfig.credential_ref` 指向 `credential_ref` 表（含 base_url、default_model，v6 加的列），密钥本体在 AES-256-GCM + Argon2id vault。
- 后端三种（data/llm/）：`openai_compat`、`anthropic`、`harness_cli`（本机已登录 claude/kimi CLI 当 LLM，零配置默认路径）；`LLMRouter` 有序降级链，降级可见。
- **刻意只读**（service.py:14-15）；**LLM 协议层无 tool_use 概念**——`LLMMessage` 仅 role/content（data/llm/backend.py:67），后端不做 tool loop。

### 1.4 Web 前端（React 18 + TS + Vite + Zustand + @xyflow/react v12）

- 导航集中在 `web/src/components/AppShell.tsx:22-31` 的 `NAV` 常量；路由在 `App.tsx`（HashRouter）；新栏目 = NAV 加项 + Route 加条。
- **已有完整聊天 UI**：`components/AssistantPanel.tsx`（676 行：气泡、流式增量、reasoning 折叠、Enter 发送、贴底自动滚动、宽度拖拽）；CSS 类（`chat-bubble--*`、`chat-scroll`、`chat-md`、`chat-reasoning`）已在 global.css。
- 流式基建：`store/assistant.ts:263-295` 的 `wireAssistant()` = chunk 累积 + `assistant_message` 到达时 REST 对账重拉；订阅入口 `store/connection.ts` 的 `onKernelPush`/`onKernelReconnect`。
- 图可视化：`graph/WorkflowCanvas.tsx`（可编辑 DAG：拖拽摆位、连线、Delete、MiniMap——拖拽交互范本）、`graph/ExecutionCanvas.tsx`（只读着色）、`graph/layout.ts`（自写确定性分层布局，多根天然兼容）。
- REST 封装 `api/client.ts`（统一 ApiError 分类）+ 端点分组 `api/endpoints.ts`。
- **文件树能力前后端均为零**；后端无任何接受用户路径的端点，无任何 path traversal 防护实现。

---

## 2. 设计决策总表（已定案）

| 编号 | 决策点 | 定案 | 核心理由 |
|---|---|---|---|
| D-A | workspace 数据存放 | **中心化**：`~/.workerbee` 单一数据目录 + `workspace` 表记 `root_dir` | 分布式会把凭据 vault 散落到各目录，违背红线 2；多实例端口/socket 冲突成本翻倍；中心化天然支持跨 workspace 视图 |
| D-B | 存量 workflow 迁移 | **自动迁入 default workspace**（root_dir = 原 `node_cwd` 或 `<data-dir>/workspace`）；Web 提供改归属操作 | 零打扰升级；改归属是简单 UPDATE + cwd 语义提示 |
| D-C | 单命令托管模式 | **默认自动 spawn supervisor**（保住"core 重启会话不死"承诺）；`--in-process` 降级；`workerbee stop` 全停 | supervisor 是核心产品承诺；编排成本可控；in_process 留作逃生门 |
| D-D | chat 工具路线 | **LLMRouter + 服务端工具循环**（扩 data/llm 协议层加 tools）；harness_cli 后端不支持 tools 时显式降级可见 | 与 fork 树天然兼容（fork=纯 DB 操作）；工具执行在服务端可过 ApprovalGateway + event_log；kimi `-p` 无运行中输入通道、harness 不支持 fork，adapter 路线会卡死 Fork 功能 |
| D-E | 新 Chat 与只读助手关系 | **分域并存**：新 chat 域（新表/新路由/新 store），AssistantPanel 保留；共用 LLMRouter/vault/脱敏/WS 基建 | 权限模型与 system prompt 完全不同，混库必然互相污染 |
| D-F | 删除子树语义 | **软删除 + 显式清空**（`deleted_at` 标记，视图默认过滤，提供清空入口） | 对话树误删成本高；消息是无资源占用的纯数据，不触碰"主动删除不可恢复"红线（该红线针对 workflow/task 的资源清理语义） |
| D-G | run 工具审批策略 | **默认逐次审批**；提供会话级临时授权（"本会话不再询问此类操作"）；危险命令模式（rm/sudo/dd/mkfs 等）**始终审批** | deny-by-default 符合审批网关既定语义（超时 deny_pause）；会话级授权消解高频审批疲劳 |

---

## 3. 功能一：Workspace 划分

### 3.1 结论
✅ 合理必要，纯增量实现。回应《架构设计 v0.02》Concern 7（共享文件工作区"最不确定"）与 D-06 的演进方向，不推翻任何定案。四项中风险最低，是其余三项的边界地基，**先行实施**。

### 3.2 设计

**Schema（migration 12）**：

```sql
CREATE TABLE workspace (
    workspace_id   TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    root_dir       TEXT NOT NULL UNIQUE,   -- 绝对路径，resolve 后存储
    created_at     TEXT NOT NULL,
    archived       INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE workflow ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'
    REFERENCES workspace(workspace_id);
-- 迁移时插入 default 行：root_dir = 原 node_cwd ?? resolved_workspace()
```

- task/attempt/session 经 workflow 间接归属，**不**给运行时表加列（避免迁移面扩散；查询走 join）。
- `managed_roots` 从单 root 扩为"所有未归档 workspace 的 root_dir 集合"（app.py:141-145 改造点）。

**cwd 解析**：Scheduler 建会话时按 workflow→workspace 取 `root_dir` 作为节点 cwd（替换 app.py:583 处全局 `node_cwd` 的单一来源；`node_cwd` 保留为 default workspace 的初值来源）。

**API**：
- `GET/POST /api/workspaces`、`GET/PATCH /api/workspaces/{id}`（改名/归档）、`POST /api/workspaces/{id}/archive`；删除 workspace 仅当无 workflow 归属，否则 409 引导先迁移。
- `GET /api/workflows` 等列表端点加 `?workspace_id=` 过滤。
- `POST /api/workflows/{id}/move`（改归属，校验目标存在且未归档）。

**启动匹配语义（与 §4 单命令联动）**：`workerbee` 在目录 D 启动时，对 cwd 做 resolve，按 `root_dir` **最长前缀匹配**已注册 workspace；无匹配则以 D 为 root 自动注册新 workspace（name 取目录名，重名加序号）。启动横幅与 Web 顶栏显示当前 workspace。

**前端**：AppShell 顶栏加 workspace 切换器（下拉，写 localStorage 记忆）；WorkflowList/TaskList 等按当前 workspace 过滤；工作流编辑器显示所属 workspace。

**验收（AC 草案）**：两 workspace 同名 workflow 互不干扰；task 执行 cwd = 所属 workspace root_dir；资源台账删除边界覆盖所有 workspace；归档 workspace 的 workflow 不可发射新任务；存量库升级后 default workspace 承接全部旧 workflow。

---

## 4. 功能二：单命令启动

### 4.1 结论
✅ 痛点真实（当前 4 步安装 + 手动两进程，用户"让 AI 帮他启动"）。增量实现，无架构级障碍。纪律：单命令 = **新薄编排层**，调用现有入口，**不得**把 supervisor 逻辑并入 core（红线 5）。

### 4.2 设计

**新命令 `workerbee serve`**（裸 `workerbee` 无参数时等价于 `serve`；`workerbee serve` 承担原"客户端"角色的入口迁移，查询类子命令保持不变）。编排流程：

```
1. 解析 data-dir（默认 ~/.workerbee，--data-dir 覆盖）与 workspace（§3.2 启动匹配）
2. 实例探测：检查 <data-dir>/core.lock（pid + port + 启动时间）
   - 存活（kill(pid,0) + GET /api/health 探活）→ 直接打印/打开带 token 的 URL，退出 0
   - 陈旧 → 清理锁文件继续（借鉴 supervisor 的 socket 存活探测模式，supervisor/server.py:244-248）
3. 端口探测：8765 被非本实例占用 → 显式报错并建议 --port，禁止裸 OSError
4. 默认 spawn `workerbee-supervisor --data-dir ...` 子进程，等 socket 就绪（超时显式失败）
   - --in-process 时跳过，core 走 HarnessRouter（明示"core 重启将打断在跑任务"）
5. 同进程内启动 core（直接调 server.main 的 run()，非 fork）
6. token 解析优先级：--token > env WORKERBEE_TOKEN > <data-dir>/token 文件（chmod 600）> 新生成并写入
   → 查询类 CLI 命令也按此顺序自动读取，消灭手动传 token
7. 打印带 token 的 URL；--open（默认开）时 webbrowser.open
8. 信号处理：SIGINT/SIGTERM → core 优雅关闭（现有链路）；supervisor **默认存活**（下次启动复用），
   --stop-supervisor-on-exit 可选跟随退出
9. `workerbee stop`：停 core（经 API/锁文件信号）+ 停 supervisor（socket 关机命令或信号）
```

**web/dist 打包**：hatch `force-include` 把 `web/dist` 打进 wheel（构建脚本顺序：先 `npm ci && npm run build` 再 `hatch build`，README 部署章同步改写）；`app.py:33` 路径解析改三级回退：包内资源（`importlib.resources` 或包相邻 `web/dist`）→ 环境变量 `WORKERBEE_WEB_DIST` → 源码布局（开发态）。

**data-dir 默认值**：`./.workerbee` → `~/.workerbee`（cli.py:93、server/main.py:36、app.py:75 三处同步；保留 `--data-dir`）。升级兼容：检测到 cwd 下存在旧 `./.workerbee` 且 `~/.workerbee` 不存在时，提示迁移而非静默另起新库（失败显式化）。

**顺带修复**：cli.py:217-243 的 `use_supervisor` 转发 bug；supervisor 包装补 `--adapter-commands`/`--socket`。

**验收（AC 草案）**：干净机器 `pip install workerbee` 后一条 `workerbee` 命令拉起可用 Web；重复执行识别已有实例直接开浏览器；Ctrl-C core 干净收束且 supervisor 存活；`workerbee stop` 后无残留进程；wheel 内嵌 dist 可被服务。

---

## 5. 功能三：Web Chat + 文件系统操作

### 5.1 结论
✅ chat 部分约 70% 复用 assistant 资产；**FS 暴露是 v0.02 清单完全没有的新能力，安全设计权重最高**，与 RES-01"清理只动托管目录"边界相邻，必须按 §5.4 的安全模型落地，不可后置。

### 5.2 Chat 域设计（分域并存，D-E）

**Schema（migration 13，与 fork 树一次到位，见 §6）**：

```sql
CREATE TABLE chat_session (
    session_id     TEXT PRIMARY KEY,
    workspace_id   TEXT NOT NULL REFERENCES workspace(workspace_id),
    title          TEXT NOT NULL,
    credential_ref TEXT,                  -- 复用 credential_ref 表；NULL = 用助手配置的后端
    model_override TEXT,
    created_at     TEXT NOT NULL,
    closed         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE chat_node (
    node_id        TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES chat_session(session_id),
    parent_id      TEXT REFERENCES chat_node(node_id),  -- NULL = 树根（森林多根）
    role           TEXT NOT NULL,          -- user / assistant / system(预留) / tool
    content        TEXT NOT NULL,
    reasoning      TEXT,
    backend        TEXT,                   -- 记录实际后端（降级可见）
    tokens_in      INTEGER, tokens_out INTEGER,
    deleted_at     TEXT,                   -- 软删除（D-F）
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_chat_node_session ON chat_node(session_id, parent_id);
```

**上下文重建**：从当前节点沿 `parent_id` 走到根，按根→叶顺序拼接为消息序列，再过滑窗截断（复用 assistant/memory.py 思路）。O(深度)，无需物化路径。

**System prompt**：新写 chat 专用 prompt（不复用 assistant 的 Workerbee 概念快照）：明示"你可以访问并操作 workspace <name>（<root_dir>）下的文件系统，通过提供的工具读/写/列目录/执行命令；写与执行操作可能需要用户审批；操作前先确认目标路径在 workspace 内"。

### 5.3 LLM 协议层 tools 扩展（D-D 落地）

- `data/llm/backend.py`：`LLMMessage` 加可选 `tool_calls` / `tool_call_id` 字段；`LLMChunk` 加 tool_call 增量类型；**全部为可选字段，assistant/capture 既有链路零改动**。
- `openai_compat.py`：映射 OpenAI tools/function-calling 协议（请求 `tools=[...]`，流式 `delta.tool_calls` 累积）。
- `anthropic.py`：映射 `tools` + `tool_use`/`tool_result` content blocks。
- `harness_cli.py`：声明 `supports_tools=False`；chat 配置落到此后端时**显式降级**：UI 横幅提示"当前后端不支持文件操作，仅纯对话"，不静默吞掉工具请求（红线 4）。
- 新增 `chat/tool_loop.py`：驱动循环——发消息+tools → 收集 tool_calls → 分发到 FS 工具执行 → 结果作为 `role=tool` 消息回注 → 直到无 tool_call 或达到轮次上限（默认 25，防爆走）；每轮中间态都落 `chat_node`（tool 调用与结果各为一个节点，parent 为触发它的 assistant 节点——这天然成为树的一部分，UI 可折叠）。

### 5.4 FS 端点与安全模型

**新路由组 `server/routes/fs.py`**（全部要求 token，自动被现有中间件覆盖）：

| 端点 | 说明 |
|---|---|
| `GET /api/fs/list?path=` | 列目录（懒加载一层；隐藏文件默认折叠；条目含 type/size/mtime/permissions） |
| `GET /api/fs/read?path=` | 读文件（默认截断 100KB，参照 artifact_content 先例；二进制探测拒绝） |
| `PUT /api/fs/write` | 写文件（存在则要求带 `expected_mtime` 做乐观并发校验，409 冲突） |
| `POST /api/fs/mkdir`、`POST /api/fs/move`、`DELETE /api/fs/delete` | 全部过审批 + event_log |
| `POST /api/fs/run` | 执行命令（cwd 限定 workspace 内；超时默认 60s 上限可配；输出截断；审批见 D-G） |

**Confinement 算法（每个端点入口必经）**：
```
real = Path(path).expanduser().resolve()          # resolve 会穿 symlink
root = workspace.root_dir.resolve()
if not real.is_relative_to(root): 403             # 同时拦住 ../ 与 symlink 逃逸
```
- 拒绝读取敏感文件名模式（`.env`、`*.pem`、`id_rsa*` 等，过 SecretRedactor 同源清单）。
- 写/删/移动/执行：先走 `ApprovalGateway`（现成，security/approval_gateway.py），审批实体带 `action_fingerprint`；全程 `event_log`（actor=api, scope=chat）。
- "用户本来的系统权限"由 workerbee 进程以该用户身份运行天然保证，**不自研权限模拟**；要防的是持 token 的 web 端越出 workspace 边界。
- `--allow-remote` 开启时文档明示 FS 端点风险升级。

### 5.5 Chat 页面（前端）

- `NAV` 加"对话"，`App.tsx` 加 `/chat` 路由；新 `store/chat.ts` + `api/endpoints.ts` 加 `chat`/`fs` 分组（照现有惯例）。
- 布局：左栏 session 列表（新建/重命名/删除，交互照 AssistantPanel 线程管理）｜中栏对话流（AssistantPanel 气泡/流式/reasoning 组件直接演化）｜右栏文件树（新组件：递归缩进列表 + 目录懒加载，纯手写 CSS 体系内实现）。
- 文件与对话联动：输入框支持 `@相对路径` 引用（前端解析为路径 token，随消息发送；后端把引用文件内容注入上下文，受 read 端点同等 confinement 约束）。
- 流式：复用 `assistant_chunk` 推送模式，新 notification kind `chat_chunk`（payload 带 session_id/node_id），REST 对账重拉纪律不变。
- **线性视图为默认**（见 §6.4，fork 树收纳为分支视图）。

**验收（AC 草案）**：自配凭证后 chat 正常流式对话；harness_cli 后端下工具能力降级可见；agent 能列目录/读文件/经审批写文件；越界路径（`../`、绝对路径、symlink）全部 403；写操作在事件日志可查；`@文件` 注入生效。

---

## 6. 功能四：Session Fork 树

### 6.1 结论
✅ 痛点真实、方案成立；**后端是零迁移包袱的低风险新设计**（chat 表原生带 parent_id，§5.2 的 schema 已一次到位）；风险集中在前端拖拽交互。强依赖 D-D 方案 A（已定案）。注意：fork 树作用在 chat 消息实体上，与 workflow 域"节点=一组 session"模型（R §5.2.2）互不干涉。

### 6.2 后端操作语义

| 操作 | 实现 | 约束 |
|---|---|---|
| 新建树根 | 插入 parent_id=NULL 的 user 节点 | — |
| 分叉 | 以任意节点为父插入新 user 节点 | 不定分叉数天然满足 |
| 删除子树 | 递归收集后代 → 全部打 `deleted_at`（D-F 软删除）；视图默认过滤 | 提供"已删除"查看与"清空"（硬删）入口；清空才不可恢复 |
| 移动/合并子树 | 单条 `UPDATE chat_node SET parent_id=?` 于子树根 | **环检测**：目标节点不得是被移子树成员（沿目标 parent 链上溯，命中子树根即拒绝 409）；跨树移动即"合并" |
| 撤销 | 各变更操作记录旧 parent_id/deleted_at，提供 `POST /api/chat/nodes/{id}/restore` | 撤销窗口 = 清空前 |

**API**：`GET /api/chat/sessions/{id}/tree`（全量节点，前端自组树；单 session 消息量有界，无需增量协议）、`POST /api/chat/sessions/{id}/messages`（带 parent_id）、`POST /api/chat/nodes/{id}/fork`、`DELETE /api/chat/nodes/{id}`（级联软删）、`POST /api/chat/nodes/{id}/move`（body: new_parent_id，环检测 409）、`POST .../restore`。

### 6.3 前端布局与交互

- **森林布局** `graph/layoutForest.ts`（新写，纯树算法比 DAG 简单）：按深度分层定 x，递归子树宽度分配 y（后序遍历：叶子占一格，父节点居中于其孩子区间），多根树纵向依次排布。不复用 `layout.ts`（其为 DAG 同层名字排序堆叠而写）。
- **分支视图**：基于 React Flow 新画布（节点=消息摘要卡，边=父子），复用 `WorkflowCanvas` 的拖拽/选中/Delete/MiniMap 范式；删除=级联软删（确认弹窗）。
- **拖拽合并（最高风险交互，自写）**：监听 `onNodeDrag`/`onNodeDragStop`；drag 期间遍历节点 bounding box 做命中检测 + 悬停高亮目标；非法目标（自己或自己的子孙）红色拒绝态；drop 后确认弹窗 → `move` API → 重拉树。撤销按钮常驻分支视图工具条。
- **降级路径先上**：拖拽合并之前先交付"点选合并"（选中子树根 → 工具条"移动到…" → 点选目标节点），拖拽作为增强迭代。这样高风险交互不阻塞主干。

### 6.4 UI 简洁性设计（回应提案"ui 必须尽可能简单、直接、实用"）

1. **默认视图 = 线性对话流**，渲染"根→当前节点"的分支路径，体验与 ChatGPT 一致；树永远收纳在可切换的"分支视图"（全屏模式或右侧抽屉），不默认面对图。
2. **分叉入口在每条消息的 hover 工具条**（"从此分叉"），无需进树视图——分叉高频、树视图低频。
3. 非当前路径的兄弟分支在线性视图中显示轻量标识（"此消息有 2 个分支 ›"），点击即切换当前分支。
4. 破坏性操作（删除子树/合并/清空）只在分支视图出现，全部带确认 + 可撤销。
5. 文件树面板与分支视图分据右栏/全屏，不同时展开，保持中栏对话流宽度。

**验收（AC 草案）**：分叉后两分支各自上下文正确（对账到 chat_node 父链路径，含中间 tool 节点）；删除子树不误删兄弟，恢复可用；移动子树成环被 409 拒绝；撤销还原；分支视图与线性视图数据一致（同一 REST 事实源）。

---

## 7. 总体结论

| 功能 | 合理性 | 实现方式 | 风险 | 定案决策 |
|---|---|---|---|---|
| Workspace 划分 | ✅ | 纯增量（migration 12 + API 过滤 + UI 切换器） | 低 | D-A 中心化、D-B 自动迁 default |
| 单命令启动 | ✅ | 增量（新薄编排层，不动三进程架构） | 中（supervisor 生命周期） | D-C 默认 spawn supervisor |
| Web Chat + FS | ✅（FS 是全新能力） | chat 约 70% 复用；FS 端点全新但边界清晰 | 中高（安全面） | D-D 服务端工具循环、D-E 分域、D-G 逐次审批+会话授权 |
| Session Fork 树 | ✅ | 后端低风险增量；前端自研森林交互 | 中（拖拽合并） | D-F 软删除；点选合并先行 |

**四项均无需架构级重设计。** 新设计全部为模块级：workspace 维度扩展、serve 编排层、LLM tools 扩展 + FS 安全模型、chat 树模型 + 森林交互，全部落在既有迁移纪律、安全层边界与前端范式内。

**需求清单登记**：按清单第 7 节规则新增编号（建议 WS-01~06、SRV-01~05、CHAT-01~08、FS-01~06、FORK-01~06）与对应 AC；workspace 提升为一等概念属对 D-06 的扩展，登记范围变更来源为 v0.03 提案。

---

## 8. 实现规划

依赖：Workspace（§3）是 Chat/FS/Fork 的边界地基；单命令（§4）独立，最先做以改善后续开发体验；Fork（§6）依赖 Chat（§5）。

### Phase 1 — 单命令启动
1. 修 cli.py 两个转发 bug。
2. web/dist 入 wheel（hatch force-include）+ `app.py:33` 三级回退路径。
3. data-dir 默认 `~/.workerbee` + 旧 `./.workerbee` 迁移提示。
4. `workerbee serve`：锁文件/端口探测 → spawn supervisor（等 socket 就绪）→ 起 core → token 文件化（chmod 600，CLI 自动读）→ 打 URL/开浏览器；`workerbee stop`。
5. AC：§4.2 验收草案全过。
> 里程碑出口：干净机器一条命令可用。

### Phase 2 — Workspace 模型
1. migration 12 + default workspace 迁移。
2. Scheduler cwd 按 workspace 解析；managed_roots 多 root。
3. workspace CRUD/move 路由 + 列表过滤。
4. 前端切换器 + 各列表过滤。
5. AC：§3.2 验收草案全过。

### Phase 3a — Chat 主干
1. migration 13（chat_session/chat_node，原生树）。
2. data/llm tools 扩展（openai_compat + anthropic；harness_cli 声明不支持 + 显式降级）。
3. `chat/tool_loop.py` + FS 工具（list/read/write/mkdir/move/delete；run 后置 3b）+ confinement。
4. 写操作过 ApprovalGateway + event_log；回复过 SecretRedactor。
5. chat 路由组 + `chat_chunk` WS 通知。
6. Chat 页面：线性视图 + session 管理 + 文件树侧栏 + `@路径` 引用。

### Phase 3b — 执行能力与加固
1. `run` 工具 + 输出截断/超时 + D-G 审批策略（逐次默认 + 会话级授权 + 危险模式始终审批）。
2. chat system prompt 定稿。
3. 红队自测进 AC：path traversal（`../`/绝对路径/symlink）、敏感文件名拒读、大文件截断、并发写 409。

### Phase 4 — Fork 树
1. 后端：fork/delete(软删)/move(环检测)/restore API + tree 查询。
2. 前端：hover 分叉入口、兄弟分支标识与切换、分支视图（layoutForest + React Flow）、点选合并。
3. 拖拽合并作为增强迭代（命中检测/非法拒绝/确认/撤销）。
4. AC：§6.3-6.4 验收草案全过。

### 横切要求（每 Phase）
- 遵守 §0.4 红线与 §0.5 组装范本；WS 推送 + REST 对账纪律；写操作幂等/CAS。
- **端到端接线 AC 测试全覆盖**（述职报告教训：静默失效是最危险模式；审批回注、Skill 注入、资源清理钩子都中过招）。
- 文档同步：架构设计 v0.02 → v0.03 增补；README 安装/启动章重写；需求清单新编号登记。
- UI 文案不引用内部类型名/章节号；commit 平实英文、无 AI 署名尾行；commit 由 AI、push 由用户。

## 9. 风险登记

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | FS 写/执行端点引入 path traversal / 越权写 | 高 | confinement 必经入口（resolve + is_relative_to + symlink 穿透）；敏感文件名拒读；写/执行过审批；红队自测进 AC（Phase 3b） |
| R2 | LLM tools 扩展波及既有 assistant/capture 链路 | 中 | 新字段全部可选；既有链路零改动；harness_cli 显式降级可见 |
| R3 | 拖拽合并子树交互复杂易出 bug | 中 | 点选合并先行；撤销机制兜底；拖拽仅作增强迭代 |
| R4 | spawn supervisor 的孤儿进程/生命周期语义混乱 | 中 | `stop` 语义明确；锁文件 + 探活复用既有实例；README 写清进程拓扑 |
| R5 | chat 与 assistant 双域并存造成用户困惑 | 低 | UI 命名区分（助手=只读问答 / 对话=可操作文件）；远期合并路线写入架构文档 |
| R6 | 会话级审批授权被滥用放大风险 | 低 | 授权仅当前会话有效、按操作类别粒度；危险命令模式始终排除在外 |
