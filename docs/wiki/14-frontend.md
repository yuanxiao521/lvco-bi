# 14 · 前端

> 根目录：`frontend/`（React 19 + Vite 8 + TypeScript 6 + Tailwind 4）。路由在 `src/App.tsx`。

## 技术栈

React 19 · react-router-dom 7 · Zustand 5 · axios（401 自动刷新 token）· ECharts 5 + Recharts · lucide-react · Tailwind 4（Vite 插件）· Vitest。

## 目录结构（`src/`）

| 目录 | 职责 |
|------|------|
| `api/` | 14 个 API 封装文件（见下），`client.ts` 统一 axios 实例 |
| `types/` | 领域类型：canvas / chart / dashboard / datasource / report / user / api；`ChartType` 14 种、`PalettePreset` 6 套配色 |
| `stores/` | Zustand：`authStore`（登录态/token）、`canvasStore`（blocks 增删/拖拽排序/选中）、`notificationsStore`（通知列表/未读/乐观更新）、`uiStore`（侧边栏/AI 面板开关） |
| `hooks/` | `useQuery`（通用数据请求）、`useSSE`（fetch+ReadableStream 解析 SSE）、`useNotificationStream`（全局 EventSource）、`useInView`（懒加载）、`useBlockAlignment`（网格吸附 8px/边缘对齐 4px/Alt 禁用） |
| `components/` | `blocks/`（CanvasBlocks / ConfigPanel / FieldPanel / AlignmentGuides）、`charts/`（ChartRenderer + echarts/ 下 10 个 ECharts 组件）、`layout/`（AppLayout / ProtectedRoute / Sidebar）、`ui/`（Toast / ErrorBoundary） |
| `pages/` | 全部业务页面（见路由表） |
| `data/` | `defaultTemplates.ts` 内置画布模板 |

## API 封装（`src/api/*.ts` ↔ 后端路由）

| 文件 | 对应后端 |
|------|---------|
| `client.ts` | axios 实例，baseURL 默认 `http://127.0.0.1:8001/api/v1`（`VITE_API_BASE_URL` 覆盖）；401 时 refresh token 自动续期（`isRefreshing`+`pendingQueue` 防并发）；`unwrapApi` 解包 `{success,data}` |
| `auth.ts` | /auth/* |
| `ai.ts` | /ai/sessions、/ai/clean、/ai/insights、/ai/polish 等 |
| `canvases.ts` | /canvases/*（含 query / ai-recommend / pin-to-dashboard / save-as-report / export/pdf） |
| `dashboards.ts` | /dashboards/* |
| `datasources.ts` | /datasources/*（upload/connect/sync/preview/tables/ai-clean） |
| `notifications.ts` | /notifications/* + `getNotificationStreamUrl()` 构造带 token 的 SSE URL |
| `permissions.ts` / `audit.ts` / `reports.ts` / `statistics.ts` / `public.ts` / `trash.ts` | 对应后端模块 |
| `types.ts` | 汇总 re-export |

## 页面路由

**公开**：`/login`、`/register`、`/forgot-password`、`/share/:token`、`/share/report/:token`。

**受保护（ProtectedRoute + AppLayout，Sidebar 定义菜单）**：

| 路径 | 页面 | 菜单 |
|------|------|------|
| `/` | FreeCanvas | 自由画布 |
| `/dashboard`、`/dashboard/:id` | Dashboard 列表 / 详情 | 仪表盘 |
| `/report-center`、`/report-center/:id` | ReportCenter 列表 / 详情 | 报表中心 |
| `/statistics` | Statistics（描述统计/相关/AI 清洗/AI 洞察） | 智能洞察 |
| `/data-source` | DataSource | 源数据管理 |
| `/ai-chat` | AIChat | AI 助手 |
| `/templates` / `/notifications` / `/trash` | Templates / Notifications / Trash | 工作空间 |
| `/account-settings` / `/permissions` / `/audit` | AccountSettings / Permissions / Audit | 系统设置 |

## SSE 流式对话（3 条通道）

### 1. AI 对话页（`pages/AIChat`）— Agent 模式
- `POST /ai/chat/stream`，body `{datasource_id, session_id, message, history}`（history 取最近 10 条，`messagesRef` 防闭包陈旧，`isAgentStreamingRef` 防并发）。
- 事件：`session_created`（同步会话列表）、`message`（delta 追加，`appendVisible` 状态机剥离 ``` 代码块）、`query_error`、`error`、`done`（携带 charts 数组 → `msg.chartData` → `ChartCard` 渲染 ECharts）。
- 无数据源时回退 `useSSE` hook 调 `/ai/sessions/{id}/messages`（事件 message/chart/done/error，支持 abort）。

### 2. 画布 AI 助手（`pages/FreeCanvas/components/AIAssistant.tsx`）
- `POST /ai/canvas/chat`，body 含 `canvas_context:{currentConfig, availableFields}`。
- 事件：`message`（流式）、`query_result`（静默）、`query_error`、`chart_config`（调 `onApplyChartConfig` 自动生成图表块）、`chart_config_error`、`error`。

### 3. 通知流（`hooks/useNotificationStream.ts`）
- 登录后全局 `new EventSource(baseURL + '/notifications/stream?token=...')`（EventSource 不支持自定义 header，token 走 query）。
- `notification` 事件 → `notificationsStore.pushNotification`；失败自动重连；Sidebar 每 60s 兜底刷新未读数。

## 图表渲染

- **`ChartRenderer.tsx`**：Recharts + ECharts 双渲染器分发。
- **`components/charts/echarts/`**：10 个 ECharts 组件（Bar/Line/Pie/Area/Scatter/Funnel/Radar/Heatmap/Sankey/HorizontalBar）+ `echartsUtils.ts`（含 `buildMultiMeasureOption` 双 Y 轴多度量 option）。
- 后端 Agent 的 `RenderChartTool` 生成的 option **镜像前端 `echartsUtils.ts` 的 `buildMultiMeasureOption`**，保证 AI 图表与手动图表样式一致。

## 关键交互细节

- **画布**：CanvasBlocks 支持拖拽排序（canvasStore.reorderBlocks）、网格吸附（useBlockAlignment 8px/4px/Alt）、选中态、AI 洞察/润色弹窗；ConfigPanel 提供图表类型/渲染器/配色/聚合方式切换与 AI 推荐图表。
- **401 处理**：client.ts 响应拦截器用 refresh token 换新（防并发刷新），失败则跳登录页。
- **统计接口**：statistics.ts 超时 120s。
