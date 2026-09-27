# Workerbee Web 客户端

多 agent harness 协作框架的运维界面（用户最终验收的这一层）。深色、高信息密度、运维工具气质。

技术栈固定：Vite + React 18 + TypeScript（strict）+ `@xyflow/react`（拓扑图）+ react-router-dom + Zustand。
样式是手写 CSS + CSS 变量（`src/styles/`），没有 UI 组件库，没有测试框架。

## 开发

```bash
cd web
npm install
npm run dev          # http://127.0.0.1:5173
```

dev server 把 `/api`（含 WebSocket，`ws: true`）代理到内核。默认目标是 `http://127.0.0.1:8765`，
需要改就设环境变量：

```bash
WORKERBEE_KERNEL=http://127.0.0.1:9000 npm run dev
```

浏览器只面对同源地址，所以 `X-Workerbee-Token` 头与 WebSocket 的 `?token=` 都不涉及跨域。

### 访问令牌

除 `/api/health` 外所有数据端点都要令牌。内核启动横幅里会打印令牌（`--token` 或 `WORKERBEE_TOKEN`
环境变量；都没给则本次启动自动生成）。

首次打开页面时 `TokenGate` 会先探一次内核：

- 返回 401 → 显示令牌输入框，填进去即可（存在 `localStorage` 的 `workerbee.token`）；
- **连不上内核 → 不拦着页面**，外壳会挂一条「无法连接内核」横幅并每 10 秒重试。
  这是刻意的：让用户看到「内核没起来」，而不是一片空白或一个假登录页。

令牌只用于 `X-Workerbee-Token` 请求头与 WebSocket 的 `?token=` 查询参数（浏览器的 WebSocket API
不能自定义请求头）。界面**从不显示、也不接收任何密钥本体**，只保存凭据引用。

## 构建

```bash
npm run build        # 先 tsc --noEmit 类型检查，再 vite build
npm run typecheck    # 只做类型检查
npm run preview      # 本地预览 dist（不带 /api 代理，需要内核另配）
```

产物在 `web/dist/`：

```
dist/index.html
dist/assets/index-*.css
dist/assets/index-*.js
```

## 让后端托管 dist

内核网关会把 `web/dist` 挂到 `/`（`workerbee/server/app.py` 的 `_install_frontend`，
默认路径就是本目录的 `dist`）：

```bash
cd web && npm install && npm run build     # 先出产物
cd .. && workerbee-server                  # 再启动内核，然后访问 http://127.0.0.1:8765/
```

产物不存在时网关返回一张解释页（写着「这里本该是前端界面」并给出构建命令），**不是 404**。
`/api/*` 优先匹配，其余路径交给静态托管；挂载用 `html=True`，但**没有 SPA fallback**——
这正是不用 history 路由、改用 `HashRouter` 的原因：深链接形如
`http://127.0.0.1:8765/#/tasks/<id>`，直接刷新也不会 404。

## 代码结构

```
src/
  api/          内核契约的唯一落点
    types.ts      后端 schemas.py / core/domain/*.py 的类型镜像（字段名逐字对齐）
    endpoints.ts  路径封装（与 workerbee/server 路由一一对应）
    client.ts     请求、令牌、ApiError（unreachable / unauthorized / conflict / unprocessable …）
    ws.ts         事件推送通道：指数退避重连 + resume 补齐水位
    guards.ts     unknown → 具体类型的读取与规范化
  store/        zustand：连接状态、需处理项、系统状态
  graph/        React Flow 画布
    WorkflowCanvas.tsx   定义图编辑器（停用节点灰化 + 虚线边框，仍可编辑）
    ExecutionCanvas.tsx  实际执行图（画的是钉扎快照；过渡态虚线 + 原因文字）
    layout.ts            分层布局（保留已有 ui_position）
  components/   少量通用组件 + 启停流程 / 节点检查器 / 队列 / 审批 / 任务控制
  pages/        12 个界面
  labels.ts     后端枚举 → 中文文案 + 视觉分级（一一对应，不新增状态）
  styles/       设计令牌 + 全局类
```

### 注册表路径：只有一个前缀

注册表四类资源（harness / 凭据 / Skill / 工具）的路径就是
`/api/harnesses`、`/api/credentials`、`/api/skills`、`/api/tools`
（`workerbee/server/routes/registry.py`）。

早期客户端为了兼容内核重构中的 `/api/registry/*` 旧前缀，做过一层「先试新前缀、404 再
回退旧前缀」的 `REGISTRY_BASES` 逻辑，**已删除**：两套前缀并存时，出问题第一个要问的
就是「现在到底哪套在生效」，回退逻辑本身成了下一个困惑源。现在 `endpoints.ts` 只保留
唯一的 `/api/...` 路径。

`listRequest` 仍然留着，但它的职责只是**形状容忍**：内核可能返回裸数组，也可能返回
`{<key>: [...]}` 或 `{items: [...]}` 信封，三种都能读。读不出形状时抛 `kind: 'parse'`
的 `ApiError` 并说明原因——**不把失败当空数据**，界面照常显示「读不到」，不会显示成空列表。

### 三条纪律（改代码前先读）

1. **状态文案只能来自后端枚举。** `TaskState` / `StageState` 的中文名集中在 `labels.ts`，
   不做合并、不造新状态（没有「loading」这种东西）。拿不准的取值经 `asTaskState` / `asStageState`
   回落到一个显式值，绝不猜成「运行中」。
2. **未知 ≠ 零。** 取不到的用量、计数、耗时显示「未知」而不是 0（`CountOrUnknown`）。
3. **服务端是权威。** 不做乐观覆盖：保存带 `base_revision_seq`，冲突就摆 diff 让用户决定；
   调序后显示的是响应的 `effective_order`，不是本地拖拽结果。

## 界面清单

流程列表 / 拓扑编辑器 / 任务列表 / 任务详情 / 实际执行图 / 会话 / 注册表 / 模板 / 存储，
外加顶栏的「需处理」抽屉（审批、失败任务、状态不明阶段、未清理资源）。
路由用 hash：`#/workflows`、`#/workflows/:id`、`#/tasks`、`#/tasks/:id`、`#/execution`、
`#/execution/:id`、`#/sessions`、`#/registry`、`#/templates`、`#/storage`。

### 会话台账（`#/sessions`）

读 `GET /api/sessions`（只读），回答排障时最常问的两个问题：「这个任务还连着哪个
session？」「它还活着吗？」。支持从任务详情/执行图带 `?task_id=<id>` 跳进来直接筛。

三条纪律：

- `state` 是台账列，**不是**内核枚举。只翻译代码里确实出现过的取值
  （`alive` / `lost` / `disposed` / `ended`，见 `labels.ts` 的 `SESSION_STATE_LABELS`），
  其余**原样显示英文**——遇到不认识的会话状态，用户要的是原文而不是猜出来的中文。
- 心跳超时**只在时间戳自带时区时**才判定（`Z` 或 `±hh:mm`）。裸时间串会被浏览器按本地
  时区解析，跨时区能差若干小时，据此报「心跳停滞」是假警报；判不了就不判，只显示时间。
  阈值 60 秒来自 supervisor 的 5 秒心跳循环（`_heartbeat_loop`）。
- 该端点属于内核新增能力，尚未落地时页面显示 404 横幅并说明「这里如实报读不到，
  不会显示成没有会话」，不渲染空表冒充「没有会话」。
