import { create } from "zustand";
import { listCanvasSessions, listMessages } from "../api/ai";
import { tokenStore } from "../api/client";
import type { AISession } from "../types/api";
import { mapProgressStatus, TOOL_STAGE } from "../pages/FreeCanvas/components/ActivityFeed";
import type { AgentMeta, FeedStep } from "../pages/FreeCanvas/components/ActivityFeed";

// ============================================================
// 画布助手全局状态 store
//
// 为什么放全局 store（而不是组件内 state）：
// 画布助手的流式请求（SSE）与对话状态生命周期应当长于 FreeCanvas 页面本身。
// 用户切到其它路由时 AIAssistant 组件会被卸载，若状态/流绑定在组件上，
// 组件卸载即断流、切回即全部丢失。提升到模块级 store 后：
//  - 组件卸载不中断 SSE，任务继续在后台跑；
//  - 切回画布时直接恢复流式状态（消息、步骤时间线、meta）。
// ============================================================

/** 聊天消息结构（迁移自 AIAssistant） */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export interface CanvasFieldMeta {
  name: string;
  data_type: string;
  category?: string;
}

export interface CanvasChartConfig {
  chartType?: string;
  dimensions?: string[];
  measures?: Array<{ field: string; agg: string }>;
}

/**
 * 一次发送所需的画布上下文快照。
 * 由组件在事件触发时（当前渲染闭包）构建传入，store 不依赖组件生命周期。
 */
export interface CanvasAssistantCtx {
  canvasId: string | null;
  datasourceId: string | null;
  fieldMeta: CanvasFieldMeta[] | null;
  canvasBlocks?: Array<Record<string, any>>;
  currentDimensions?: string[];
  currentMeasures?: Array<{ field: string; agg: string }>;
  currentChartType?: string;
  allDatasources?: Array<{ id: string; name: string; fields?: CanvasFieldMeta[] }>;
  onCanvasAction?: (action: any) => void;
  onApplyChartConfig?: (config: CanvasChartConfig) => void;
  ensureCanvas?: () => Promise<string>;
}

/** 生成稳定唯一的消息 id */
function makeMsgId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 根据数据源状态构建欢迎语 */
function buildWelcome(
  datasourceId: string | null,
  fieldMeta: CanvasFieldMeta[] | null,
): ChatMessage {
  return {
    id: "welcome",
    role: "assistant",
    content: datasourceId && fieldMeta?.length
      ? `你好！我已了解当前数据源，共有 ${fieldMeta.length} 个字段。你可以让我帮你分析数据、推荐图表。`
      : "你好！我是 AI 画布助手。先选择数据源，我就能帮你分析数据和配置图表。",
  };
}


// ---- 模块级"同步互斥"与流控制器（组件卸载后依旧存活） ----
let streamingLock = false;          // 并发互斥：同步级，替代组件内 streamingRef
let newSession = false;             // 下一次请求是否强制新建会话
let runSeq = 0;                     // 步骤自增 id
let activeCancel: (() => void) | null = null; // 当前流的 reader.cancel
// SSE 空闲超时（毫秒）：后端每步最多 45s（_STEP_TIMEOUT），90s 静默必属卡住
const STREAM_IDLE_MS = 90_000;
let abortRequested = false;         // 用户点了「停止」：收尾时标注"已停止"
// 当前在跑的流属于哪个画布：切画布时据此判断是否需要中止旧流
// （否则旧流的消息/进度/落块动作会写进新画布，历史与画布内容双双串台）
let activeStreamCanvasId: string | null = null;
let canvasSessionsSeq = 0;          // 会话列表请求序号（防异步竞态：旧请求结果不得覆盖新画布）
let activeCanvasId: string | null = null; // 当前激活画布 id（跨组件同步，供异步回调校验）

interface CanvasAssistantStore {
  // 会话与对话状态
  messages: ChatMessage[];
  steps: FeedStep[];
  meta: AgentMeta | null;
  canvasSessions: AISession[];
  curSessionId: string | null;
  isStreaming: boolean;
  sessionsLoaded: boolean;
  open: boolean; // 面板展开状态提升，切路由回来仍保持打开

  setOpen: (open: boolean) => void;
  /** 数据源/字段变化时刷新欢迎语（仅当首条仍是 welcome） */
  syncWelcome: (ctx: CanvasAssistantCtx) => void;
  /** 拉取当前画布的会话列表 */
  refreshSessions: (canvasId: string | null) => Promise<void>;
  /** 真正切换画布：清空会话状态防串记忆 */
  resetForCanvas: (ctx: CanvasAssistantCtx) => void;
  /** 挂载/会话列表就绪后恢复最近会话的历史（切回画布时恢复对话） */
  onCanvasReady: (canvasId: string | null) => Promise<void>;
  /** 轻量切换当前会话引用 */
  applySession: (sid: string | null) => void;
  /** 新对话（1:1 画布会话：清空界面，下一次发送后端复用唯一会话并清空历史） */
  newConversation: (ctx: CanvasAssistantCtx) => void;
  /** 发送消息：首步校验 + SSE 流式消费（核心逻辑迁移自 AIAssistant.handleSend）。
   *  uiAction：HITL 确认卡片动作（{type:"confirm"|"cancel"}），点按钮时携带，后端守卫据此短路决策。 */
  send: (content: string, ctx: CanvasAssistantCtx, uiAction?: { type: string } | null) => Promise<void>;
  /** 待确认卡片（后端 confirm_request 事件的载荷）；null = 无待确认。用户点击按钮或取消后清除 */
  pendingConfirm: { goal: string; question: string } | null;
  clearPendingConfirm: () => void;
}

export const useCanvasAssistantStore = create<CanvasAssistantStore>()((set, get) => ({
  messages: [buildWelcome(null, null)],
  steps: [],
  meta: null,
  canvasSessions: [],
  curSessionId: null,
  isStreaming: false,
  sessionsLoaded: false,
  open: false,
  pendingConfirm: null,

  clearPendingConfirm: () => set({ pendingConfirm: null }),

  setOpen: (open) => set({ open }),

  syncWelcome: (ctx) => {
    const { messages } = get();
    if (messages[0]?.id !== "welcome") return;
    const { datasourceId, fieldMeta, allDatasources } = ctx;
    const first = buildWelcome(datasourceId, fieldMeta);
    let content = first.content;
    if (datasourceId && fieldMeta?.length) {
      const names = fieldMeta.slice(0, 8).map((f) => f.name);
      content = `你好！我已了解当前数据源，共有 ${fieldMeta.length} 个字段：${names.join("、")}${fieldMeta.length > 8 ? "等" : ""}。\n\n- 推荐适合的图表类型\n- 分析数据分布\n- 查找数据规律`;
    } else if (allDatasources?.length) {
      content = `你好！当前有以下数据源可用：${allDatasources.map((d) => d.name).join("、")}。\n\n请选择一个数据源开始分析，或直接告诉我你想分析什么数据。`;
    } else {
      return;
    }
    set({
      messages: [
        { id: "welcome", role: "assistant", content },
        ...messages.slice(1),
      ],
    });
  },

  refreshSessions: async (canvasId) => {
    if (!canvasId) {
      activeCanvasId = null;
      set({ canvasSessions: [] });
      return;
    }
    // 请求序号 +1：只有"发起时对应的画布仍是激活态"的序号才允许写入，
    // 快速来回切换画布时旧请求返回不得覆盖新画布的会话列表。
    const seq = ++canvasSessionsSeq;
    activeCanvasId = canvasId;
    try {
      const list = await listCanvasSessions(canvasId);
      if (seq === canvasSessionsSeq && activeCanvasId === canvasId) {
        set({ canvasSessions: list });
      }
    } catch {
      if (seq === canvasSessionsSeq && activeCanvasId === canvasId) {
        set({ canvasSessions: [] });
      }
    }
  },

  resetForCanvas: (ctx) => {
    const cid = ctx.canvasId ?? null;
    set({ pendingConfirm: null });
    // 同画布重入守卫：首条消息发送时 ensureCanvas 会创建画布（canvasId: null → 新 id），
    // 组件随之触发本函数。此刻本画布的流正在跑（streamingLock 已置位，但
    // activeStreamCanvasId 要到 ensureCanvas 之后才赋值，故 null 也视为"本流初始化中"）——
    // 清空 messages 会让流事件的 patchAssistant 按找不到的 id 静默失效，
    // assistant 回复整条丢失（实测首条消息必现）。同画布 ≠ 串台，保留现场返回。
    if (streamingLock && cid && (activeStreamCanvasId === cid || !activeStreamCanvasId)) {
      activeCanvasId = cid;
      return;
    }
    // 进行中的流若属于别的画布：先中止，避免它继续往当前画布写消息/进度/落块
    if (streamingLock && activeStreamCanvasId !== cid) {
      try { activeCancel?.(); } catch { /* ignore */ }
      activeCancel = null;
      streamingLock = false;
    }
    activeCanvasId = cid;
    canvasSessionsSeq += 1; // 使在途的旧列表请求结果全部失效
    set({
      curSessionId: null,
      sessionsLoaded: false,
      // 必须清掉上一画布的会话列表：否则 onCanvasReady 会拿它当本画布的列表，
      // 采纳上一个画布的会话 id 并加载它的历史 → 会话历史串到另一张画布
      canvasSessions: [],
      steps: [],
      meta: null,
      messages: [buildWelcome(ctx.datasourceId, ctx.fieldMeta)],
      isStreaming: false,
    });
    newSession = false;
    if (cid) {
      void get().refreshSessions(cid);
    }
  },

  onCanvasReady: async (canvasId) => {
    if (!canvasId || get().sessionsLoaded) return;
    // 流进行中禁止历史恢复：DB 里的 assistant 正文要等流结束才落库，此刻恢复会把
    // 流中的 assistant 占位整体覆盖（patchAssistant 随后全部 MISS，回复整条丢失）。
    // 流结束后组件 effect 再触发时（切走切回/列表刷新）DB 已完整，恢复无害。
    if (get().isStreaming) return;
    // 画布激活态校验：若期间用户已切走或重设为别的画布，本回调返回
    if (activeCanvasId !== canvasId) return;
    const { canvasSessions } = get();
    if (canvasSessions.length > 0 && !get().curSessionId) {
      get().applySession(canvasSessions[0].id);
    }
    const sid = get().curSessionId;
    if (!sid) return; // 会话列表还没加载完，等下次 effect 再处理
    set({ sessionsLoaded: true }); // 有会话 ID 后才标记已加载
    try {
      const msgs = await listMessages(sid);
      if (activeCanvasId !== canvasId) return; // 加载期间切走了，丢弃
      if (msgs.length > 0) {
        set({
          messages: msgs
            .filter((m) => !(m.role === "assistant" && !m.content.trim())) // 过滤未完成/空占位
            .map((m) => ({
              id: m.id,
              role: m.role as "user" | "assistant",
              content: m.content,
            })),
        });
      }
    } catch {
      if (activeCanvasId === canvasId) {
        get().applySession(null);
      }
    }
  },

  applySession: (sid) => set({ curSessionId: sid }),

  newConversation: (ctx: CanvasAssistantCtx) => {
    if (streamingLock) return;
    get().applySession(null);
    newSession = true;
    set({ steps: [], meta: null, messages: [buildWelcome(ctx.datasourceId, ctx.fieldMeta)] });
  },

  send: async (content, ctx, uiAction) => {
    // [关键] 同步级互斥：秒级连点不会绕过
    if (streamingLock) return;
    const trimmed = (content ?? "").trim();
    if (!trimmed) return;

    // 未选择数据源时给出提示，不发起请求
    if (!ctx.datasourceId) {
      set((s) => ({
        messages: [...s.messages, { id: makeMsgId("e"), role: "assistant", content: "请先在左侧选择一个数据源" }],
      }));
      return;
    }

    streamingLock = true;
    set({ isStreaming: true });

    let effCanvasId = ctx.canvasId;
    if (!effCanvasId && ctx.ensureCanvas) {
      try {
        effCanvasId = await ctx.ensureCanvas();
      } catch {
        // 创建失败按草稿继续，后端以 canvas_id=null 兜底
      }
    }
    activeStreamCanvasId = effCanvasId ?? null;  // 本流归属画布：切画布时据此中止

    const assistantId = makeMsgId("a");
    set((s) => ({
      messages: [
        ...s.messages,
        { id: makeMsgId("u"), role: "user", content: trimmed },
        { id: assistantId, role: "assistant", content: "" },
      ],
    }));

    let assistantContent = "";
    let localCanvasActions = 0;
    // 流空闲看门狗状态：写在 try 外，finally 才能读到（try 块内的 let 不跨块可见）
    let stallTimer: ReturnType<typeof setTimeout> | null = null;
    let stalled = false;

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1";

    // 校验当前图表的维度和度量字段是否在数据源字段列表中，过滤掉已删除的字段
    const validFieldNames = new Set(
      (ctx.fieldMeta ?? []).map((f) => f.name).concat((ctx.fieldMeta ?? []).map((f) => f.name.toLowerCase())),
    );
    const cleanDims = (ctx.currentDimensions ?? []).filter(
      (d) => validFieldNames.has(d) || validFieldNames.has(d.toLowerCase()),
    );
    const cleanMeasures = (ctx.currentMeasures ?? []).filter(
      (m) => validFieldNames.has(m.field) || validFieldNames.has(m.field.toLowerCase()),
    );
    const hasValidCurrentConfig = cleanDims.length > 0 || cleanMeasures.length > 0;

    const canvasContext: Record<string, unknown> = {};
    if (hasValidCurrentConfig) {
      canvasContext.currentConfig = { dimensions: cleanDims, measures: cleanMeasures, chartType: ctx.currentChartType };
    }
    canvasContext.availableFields = (ctx.fieldMeta ?? []).map((f) => ({
      name: f.name, data_type: f.data_type, category: f.category,
    }));
    if (Array.isArray(ctx.canvasBlocks) && ctx.canvasBlocks.length) {
      canvasContext.blocks = ctx.canvasBlocks
        .filter((b) => b && b.type === "chart")
        .map((b) => ({
          block_id: b.blockId,
          title: b.title,
          chartType: b.chartType,
          dimensions: b.queryConfig?.dimensions ?? b.dimensions ?? [],
          measures: b.queryConfig?.measures ?? b.measures ?? [],
          // 布局感知：带上块坐标，LLM 据此判断画布是否整齐、是否需要整理布局
          x: b.x, y: b.y, width: b.width, height: b.height,
        }));
    }
    set({ steps: [], meta: null }); // 每次新任务重置工作台步骤时间线 + 流程元信息

    const patchAssistant = (content: string) =>
      set((s) => ({ messages: s.messages.map((m) => (m.id === assistantId ? { ...m, content } : m)) }));

    try {
      const response = await fetch(`${baseUrl}/ai/canvas/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          datasource_id: ctx.datasourceId,
          canvas_id: effCanvasId ?? null,
          session_id: get().curSessionId,
          new_session: newSession || false,
          message: trimmed,
          canvas_context: canvasContext,
          ui_action: uiAction ?? null,
        }),
      });
      newSession = false;

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err?.detail?.message || err?.error?.message || `HTTP ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      activeCancel = () => {
        try { reader.cancel(); } catch { /* ignore */ }
      };

      const decoder = new TextDecoder();
      let buffer = "";

      // 空闲看门狗：SSE 超过 IDLE_MS 没有任何字节 → 判定后端卡住，主动断开并提示
      // （后端 Agent 停在某个 await 时，流会一直挂着且不报错，界面就会永远"思考中"）
      const armStall = () => {
        if (stallTimer) clearTimeout(stallTimer);
        stallTimer = setTimeout(() => {
          stalled = true;
          try { reader.cancel(); } catch { /* ignore */ }
        }, STREAM_IDLE_MS);
      };

      const consumeLine = (line: string) => {
        if (!line.startsWith("data: ")) return;
        const jsonStr = line.slice(6).trim();
        if (!jsonStr) return;
        let event: any;
        try { event = JSON.parse(jsonStr); } catch { return; }
        switch (event.type) {
          case "intent":
            set((s) => ({ meta: { ...(s.meta ?? {}), intent: event.intent, intentConfidence: event.confidence, degraded: event.degraded ?? s.meta?.degraded } }));
            break;
          case "decision":
            set((s) => ({ meta: { ...(s.meta ?? {}), decision: event.action, decisionTool: event.tool, decisionReason: event.reason, degraded: event.degraded ?? s.meta?.degraded } }));
            break;
          case "progress": {
            const pIdx = Number(event.index ?? 0);
            const pTotal = Number(event.total ?? 0);
            // 带 round 前缀，避免多轮循环下第二轮 index 覆盖第一轮步骤
            const pRound = Number(event.round ?? 0);
            // react 模式的 per-tool progress（total=0）与 tool_call/tool_result 完全冗余：
            // 每个工具各建一个"执行 xxx"step，把执行记录压成一串无层级平铺行。忽略之，
            // 工具状态由 tool_call/tool_result 驱动；编排器的 plan 级 progress（total>0，
            // 有真实的 1/n 步骤语义）仍保留建 step。
            if (!(pTotal > 0)) break;
            const stepId = `p${pRound}_${pIdx}`;
            set((s) => {
              const base = {
                title: String(event.title ?? "执行中"),
                // 后端 index 为 1-based（lead_perception idx 从 1 起步），直接展示，不要 +1
                seq: pTotal > 0 ? `${pIdx}/${pTotal}` : undefined,
                emphasis: event.status === "start",
              };
              const existing = s.steps.find((st) => st.id === stepId);
              if (existing) {
                return { steps: s.steps.map((st) => (st.id === stepId ? { ...st, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: st.tools } : st)) };
              }
              return { steps: [...s.steps, { id: stepId, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: [] }] };
            });
            break;
          }
          case "message": {
            const delta = event.delta ?? "";
            if (delta.includes("已在画布生成分析报告") || delta.includes("图表与叙事段落已就位")) break;
            assistantContent += delta;
            patchAssistant(assistantContent);
            break;
          }
          case "tool_call": {
            const name = event?.name ?? "工具";
            const stage = TOOL_STAGE(name);
            set((s) => {
              const next = s.steps.slice();
              // 阶段分桶：找本次流内最后一个同阶段 step 聚合（"阶段 → 工具"两级层级），
              // 替代旧的"永远 append 到最后一个 step"——那会把所有工具塞进同一个无语义的
              // "执行 分析执行" step，整个执行记录变成一串平铺的同名行。
              for (let i = next.length - 1; i >= 0; i--) {
                if (next[i].title === stage) {
                  next[i] = {
                    ...next[i],
                    tools: [...next[i].tools, { name, args: event.args, status: "run" }],
                    status: next[i].status === "done" ? "run" : next[i].status,
                  };
                  return { steps: next };
                }
              }
              runSeq += 1;
              next.push({ id: `${runSeq}`, title: stage, status: "run", tools: [{ name, args: event.args, status: "run" }] });
              return { steps: next };
            });
            break;
          }
          case "tool_result": {
            const isErr = (() => {
              try {
                const r = event.result ? JSON.parse(event.result) : null;
                return !!(r && r.error);
              } catch { return false; }
            })();
            const tName = event.name ?? "";
            const settle = (st: FeedStep): FeedStep => {
              // step 内全部工具已收尾 → step 置 done（不等 done 事件统一收敛，实时反映）
              const allSettled = st.tools.length > 0 && st.tools.every((t) => t.status !== "run");
              const anyErr = st.tools.some((t) => t.status === "err");
              if (allSettled) return { ...st, status: anyErr && st.status === "run" ? "failed" : "done" };
              return st;
            };
            set((s) => {
              for (let si = s.steps.length - 1; si >= 0; si--) {
                const step = s.steps[si];
                for (let ti = step.tools.length - 1; ti >= 0; ti--) {
                  if (step.tools[ti].name === tName && step.tools[ti].status === "run") {
                    const next = s.steps.slice();
                    const newTools = step.tools.slice();
                    newTools[ti] = { ...newTools[ti], result: event.result, status: isErr ? "err" : "ok" };
                    next[si] = settle({ ...step, tools: newTools });
                    return { steps: next };
                  }
                }
              }
              return {
                steps: s.steps.map((st, i) =>
                  i === s.steps.length - 1
                    ? settle({ ...st, tools: st.tools.map((t, j) => (j === st.tools.length - 1 ? { ...t, result: event.result, status: isErr ? "err" : "ok" } : t)) })
                    : st,
                ),
              };
            });
            break;
          }
          case "confirm_request": {
            // HITL 确认卡片：后端 ask_user(ask_kind=confirm) 的载荷。
            // 用户点按钮 → 带 ui_action 的下一请求由后端守卫短路恢复/取消。
            set({
              pendingConfirm: {
                goal: String(event.goal ?? ""),
                question: String(event.question ?? ""),
              },
            });
            break;
          }
          case "canvas_action": {
            localCanvasActions += 1;
            // 画布动作不再拼进对话正文：逐条 += 会产出「已删除块: 块 xxx 已删除」
            // 这类冗余回执（且 ACTION_LABEL 与 desc 表述重复），污染对话观感。
            // 工具调用本身已由 tool_call / tool_result 记入执行记录（steps），
            // 画布上的增删改本身也是可见反馈，无需在正文里再念一遍。
            if (ctx.onCanvasAction) ctx.onCanvasAction(event);
            break;
          }
          case "query_result":
            patchAssistant(assistantContent);
            break;
          case "query_error":
            assistantContent += `\n\n> ${event.message}`;
            patchAssistant(assistantContent);
            break;
          case "chart_config":
            assistantContent += `\n\n[图表配置已应用]`;
            patchAssistant(assistantContent);
            if (ctx.onApplyChartConfig && event.config) ctx.onApplyChartConfig(event.config);
            break;
          case "chart_config_error":
            assistantContent += `\n\n> ${event.message}`;
            patchAssistant(assistantContent);
            break;
          case "error":
            assistantContent += `\n\n> ${event.message}`;
            patchAssistant(assistantContent);
            break;
          case "session_created":
            get().applySession(event.session_id);
            void get().refreshSessions(effCanvasId);
            break;
          case "done": {
            // done 收敛：工作台所有 step 置为完成（run/wait → done），避免残留"执行中"或"2/x"
            set((s) => ({
              steps: s.steps.map((st) =>
                st.status === "run" || st.status === "wait"
                  ? {
                      ...st,
                      status: "done",
                      tools: st.tools.map((t) => (t.status === "run" ? { ...t, status: "ok" } : t)),
                    }
                  : st,
              ),
            }));
            streamingLock = false;
            set({ isStreaming: false });
            break;
          }
          default:
            break;
        }
      };

      while (true) {
        armStall();
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) consumeLine(line);
      }

      // 循环结束后处理 buffer 中残留的最后一行（SSE 末尾可能没有换行符）
      if (buffer && buffer.trim()) {
        for (const line of buffer.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;
          try {
            const event = JSON.parse(jsonStr);
            if (event.type === "done") {
              set((s) => ({
                steps: s.steps.map((st) => (st.status === "run" ? { ...st, status: "done", tools: st.tools } : st)),
              }));
              streamingLock = false;
              set({ isStreaming: false });
            }
          } catch { /* ignore */ }
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "未知错误";
      patchAssistant(`[连接失败] ${msg}`);
    } finally {
      // 流结束但 AI 没有生成任何文本 → 用本轮画布动作数生成兜底文案
      if (!assistantContent.trim() && localCanvasActions > 0) {
        assistantContent = `本次分析通过画布工具完成：在画布执行 ${localCanvasActions} 次落块操作，请查看画布内容与工作台执行记录。`;
        patchAssistant(assistantContent);
      }
      activeCancel = null;
      if (stallTimer) clearTimeout(stallTimer);
      stallTimer = null;
      if (stalled) {
        // 后端长时间静默：明确告知已中断，而不是让气泡永远停在"思考中"
        patchAssistant(
          (assistantContent ? assistantContent + "\n\n" : "") +
            `> 响应超时：后端 ${Math.round(STREAM_IDLE_MS / 1000)} 秒无输出，已中断本轮。请重试或换个问法。`,
        );
      } else if (abortRequested && !assistantContent.trim()) {
        patchAssistant("> 已停止本轮生成。");
      }
      abortRequested = false;
      activeStreamCanvasId = null;
      streamingLock = false;
      set({ isStreaming: false });
    }
  },
}));

/** 读取 store 当前状态归属的画布 id（供组件判断"是否需要重置会话状态"）。
 *
 * 为什么需要：AIAssistant 在路由切换/重新挂载时，组件内的 prev 引用会重置为 null，
 * 单看 prev 无法区分「同一画布重新挂载」和「从另一个画布切过来」，
 * 后者若不重置就会把上一个画布的历史显示到本画布上。
 */
export function getActiveCanvasId(): string | null {
  return activeCanvasId;
}

/** 主动中止当前流（组件卸载不再调用；供"停止"按钮使用） */
export function abortCanvasStream(): void {
  abortRequested = true;
  try { activeCancel?.(); } catch { /* ignore */ }
  activeCancel = null;
  streamingLock = false;
  useCanvasAssistantStore.setState({ isStreaming: false });
}