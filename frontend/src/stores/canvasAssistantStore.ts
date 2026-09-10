import { create } from "zustand";
import { listCanvasSessions, listMessages } from "../api/ai";
import { tokenStore } from "../api/client";
import type { AISession } from "../types/api";
import { mapProgressStatus } from "../pages/FreeCanvas/components/ActivityFeed";
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

/** 工具名 → 中文名 */
function TOOL_FALLBACK_NAME(name: string): string {
  const map: Record<string, string> = {
    add_chart_block: "新增图表", add_text_block: "写文本", add_h1: "写标题",
    add_h2: "写章节", update_chart_block: "改图表",
  };
  return map[name] ?? name;
}

/** canvas_action 类型 → 动作中文 */
const ACTION_LABEL: Record<string, string> = {
  add_chart_block: "添加图表", add_text_block: "添加文本", update_chart_block: "更新图表",
  remove_block: "删除块", arrange_layout: "自动布局",
};

/** 根据 canvas_action 生成一段可读的描述文本 */
function actionDesc(action: any): string {
  const block = action?.block;
  const title = block?.title || block?.content || "";
  const target = action?.blockId || "";
  switch (action?.action) {
    case "add_chart_block": return `「${title}」已添加`;
    case "add_text_block": return `「${title}」已添加`;
    case "update_chart_block": return `块 ${target} 已更新`;
    case "remove_block": return `块 ${target} 已删除`;
    default: return "";
  }
}

// ---- 模块级"同步互斥"与流控制器（组件卸载后依旧存活） ----
let streamingLock = false;          // 并发互斥：同步级，替代组件内 streamingRef
let newSession = false;             // 下一次请求是否强制新建会话
let runSeq = 0;                     // 步骤自增 id
let activeCancel: (() => void) | null = null; // 当前流的 reader.cancel

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
  /** 切换画布内历史会话 */
  switchSession: (sid: string, ctx: CanvasAssistantCtx) => Promise<void>;
  /** 新对话（另开一条画布内对话） */
  newConversation: (ctx: CanvasAssistantCtx) => void;
  /** 发送消息：首步校验 + SSE 流式消费（核心逻辑迁移自 AIAssistant.handleSend） */
  send: (content: string, ctx: CanvasAssistantCtx) => Promise<void>;
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
      set({ canvasSessions: [] });
      return;
    }
    try {
      const list = await listCanvasSessions(canvasId);
      set({ canvasSessions: list });
    } catch {
      set({ canvasSessions: [] });
    }
  },

  resetForCanvas: (ctx) => {
    const { messages, steps, meta } = get();
    void messages; void steps; void meta;
    set({
      curSessionId: null,
      sessionsLoaded: false,
      steps: [],
      meta: null,
      messages: [buildWelcome(ctx.datasourceId, ctx.fieldMeta)],
    });
    newSession = false;
    void get().refreshSessions(ctx.canvasId);
  },

  onCanvasReady: async (canvasId) => {
    if (!canvasId || get().sessionsLoaded) return;
    const { canvasSessions } = get();
    if (canvasSessions.length > 0 && !get().curSessionId) {
      get().applySession(canvasSessions[0].id);
    }
    const sid = get().curSessionId;
    if (!sid) return; // 会话列表还没加载完，等下次 effect 再处理
    set({ sessionsLoaded: true }); // 有会话 ID 后才标记已加载
    try {
      const msgs = await listMessages(sid);
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
      get().applySession(null);
    }
  },

  applySession: (sid) => set({ curSessionId: sid }),

  switchSession: async (sid, ctx) => {
    if (streamingLock || !sid || sid === get().curSessionId) return;
    get().applySession(sid);
    set({ steps: [], meta: null });
    try {
      const msgs = await listMessages(sid);
      if (msgs.length > 0) {
        set({
          messages: msgs
            .filter((m) => !(m.role === "assistant" && !m.content.trim()))
            .map((m) => ({
              id: m.id,
              role: m.role as "user" | "assistant",
              content: m.content,
            })),
        });
      } else {
        set({ messages: [buildWelcome(ctx.datasourceId, ctx.fieldMeta)] });
      }
    } catch {
      get().applySession(null);
    }
  },

  newConversation: (ctx: CanvasAssistantCtx) => {
    if (streamingLock) return;
    get().applySession(null);
    newSession = true;
    set({ steps: [], meta: null, messages: [buildWelcome(ctx.datasourceId, ctx.fieldMeta)] });
  },

  send: async (content, ctx) => {
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
            set((s) => {
              const base = {
                title: String(event.title ?? "执行中"),
                seq: pTotal > 0 ? `${pIdx + 1}/${pTotal}` : undefined,
                emphasis: event.status === "start",
              };
              const existing = s.steps.find((st) => st.id === `p${pIdx}`);
              if (existing) {
                return { steps: s.steps.map((st) => (st.id === `p${pIdx}` ? { ...st, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: st.tools } : st)) };
              }
              return { steps: [...s.steps, { id: `p${pIdx}`, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: [] }] };
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
          case "tool_call":
            set((s) => {
              if (s.steps.length === 0) {
                runSeq += 1;
                return { steps: [{ id: `${runSeq}`, title: `执行 ${TOOL_FALLBACK_NAME(event?.name ?? "工具")}`, status: "run", tools: [{ name: event.name, args: event.args, status: "run" }] }] };
              }
              const next = s.steps.slice();
              const last = next[next.length - 1];
              next[next.length - 1] = { ...last, tools: [...last.tools, { name: event.name, args: event.args, status: "run" }] };
              return { steps: next };
            });
            break;
          case "tool_result": {
            const isErr = (() => {
              try {
                const r = event.result ? JSON.parse(event.result) : null;
                return !!(r && r.error);
              } catch { return false; }
            })();
            const tName = event.name ?? "";
            set((s) => {
              for (let si = s.steps.length - 1; si >= 0; si--) {
                const step = s.steps[si];
                for (let ti = step.tools.length - 1; ti >= 0; ti--) {
                  if (step.tools[ti].name === tName && step.tools[ti].status === "run") {
                    const next = s.steps.slice();
                    const newTools = step.tools.slice();
                    newTools[ti] = { ...newTools[ti], result: event.result, status: isErr ? "err" : "ok" };
                    next[si] = { ...step, tools: newTools };
                    return { steps: next };
                  }
                }
              }
              return {
                steps: s.steps.map((st, i) =>
                  i === s.steps.length - 1
                    ? { ...st, tools: st.tools.map((t, j) => (j === st.tools.length - 1 ? { ...t, result: event.result, status: isErr ? "err" : "ok" } : t)) }
                    : st,
                ),
              };
            });
            break;
          }
          case "canvas_action": {
            localCanvasActions += 1;
            assistantContent += `\n\n> 已${ACTION_LABEL[event.action] ?? event.action}: ${actionDesc(event)}\n`;
            patchAssistant(assistantContent);
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
            set((s) => ({
              steps: s.steps.map((st) => {
                const hasRunTools = st.tools.some((t) => t.status === "run");
                if (hasRunTools || st.status === "run") {
                  return {
                    ...st,
                    status: "done",
                    tools: st.tools.map((t) => (t.status === "run" ? { ...t, status: "ok" } : t)),
                  };
                }
                return st;
              }),
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
      streamingLock = false;
      set({ isStreaming: false });
    }
  },
}));

/** 主动中止当前流（组件卸载不再调用；供"停止"等 UI 使用） */
export function abortCanvasStream(): void {
  try { activeCancel?.(); } catch { /* ignore */ }
  activeCancel = null;
  streamingLock = false;
  useCanvasAssistantStore.setState({ isStreaming: false });
}