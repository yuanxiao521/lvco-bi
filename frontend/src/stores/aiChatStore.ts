import { create } from "zustand";
import { createSession, listMessages, listSessions } from "../api/ai";
import { tokenStore } from "../api/client";
import type { AISession, AIMessage } from "../types/api";
import { mapProgressStatus } from "../pages/FreeCanvas/components/ActivityFeed";
import type { AgentMeta, FeedStep } from "../pages/FreeCanvas/components/ActivityFeed";

// ============================================================
// 对话助手（AIChat）全局状态 store
//
// 与画布助手一致：SSE 流与对话状态的生命周期长于 AIChat 页面组件。
// 切到其它路由时组件卸载，但流继续在 store 内跑，切回时无缝恢复；
// 后端 /chat/stream 已做落库兜底（会话+用户消息提前 commit + 占位行 +
// finally 补存），前端这里负责把"进行中的任务"跨路由保活。
// ============================================================

/** 发送所需的上下文快照（事件触发时由组件传入最新值） */
export interface AIChatCtx {
  selectedDsId: string;
  hasDatasources: boolean;
}

// 模块级同步互斥与流控制器（组件卸载后依旧存活）
let agentStreamingLock = false; // agent 模式并发锁（同步级，替代组件内 isAgentStreamingRef）

// fallback（普通 chat）流的锁：同一时刻只允许一条流
let fallbackStreamingLock = false;

interface StreamingChart {
  chart_type: string;
  option: Record<string, unknown>;
}

export type { StreamingChart };

interface AIChatStore {
  sessions: AISession[];
  messages: AIMessage[];
  agentSteps: FeedStep[];
  agentMeta: AgentMeta | null;
  activeSessionId: string | null;
  isStreaming: boolean;    // agent / fallback 任一在流即 true
  aiNotConfigured: boolean;

  setSessions: (sessions: AISession[]) => void;
  setActiveSession: (sid: string | null) => void;
  /** 拉取会话列表（entry=chat） */
  refreshSessions: () => Promise<void>;
  /** 加载某个会话的消息（过滤"未完成"的空 assistant 占位行） */
  loadMessages: (sid: string) => Promise<void>;
  /** agent 模式流式（POST /ai/chat/stream） */
  streamChat: (sid: string | null, content: string, selectedDsId: string) => Promise<void>;
  /** fallback 普通 chat（POST /ai/sessions/{id}/messages，无数据源时） */
  sendFallback: (sid: string, content: string) => Promise<void>;
  /** 统一发送入口：无会话则先建会话，再按是否有数据源选择路径 */
  sendChat: (content: string, ctx: AIChatCtx) => Promise<void>;
  /** 清空"进行中"占位状态（如异常后手动收尾） */
  clearStream: () => void;
}

export const useAIChatStore = create<AIChatStore>()((set, get) => ({
  sessions: [],
  messages: [],
  agentSteps: [],
  agentMeta: null,
  activeSessionId: null,
  isStreaming: false,
  aiNotConfigured: false,

  setSessions: (sessions) => set({ sessions }),
  setActiveSession: (sid) => set({ activeSessionId: sid }),

  refreshSessions: async () => {
    try {
      const list = await listSessions({ entry: "chat" }); // 只列出对话助手会话（排除画布会话）
      set({ sessions: list });
    } catch {
      // silently fail
    }
  },

  loadMessages: async (sid) => {
    try {
      const msgs = await listMessages(sid);
      set({
        messages: msgs.filter((m) => !(m.role === "assistant" && !m.content.trim())),
      });
    } catch {
      // silently fail
    }
  },

  streamChat: async (sid, content, selectedDsId) => {
    if (agentStreamingLock) return;
    agentStreamingLock = true;
    set({ isStreaming: true });

    const userMsg: AIMessage = {
      id: `temp-${Date.now()}`,
      sessionId: sid || "",
      role: "user",
      content,
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    set((s) => ({ messages: [...s.messages, userMsg] }));

    let assistantContent = "";
    let visibleContent = "";
    let codeFenceState: "open" | "closed" = "closed";
    let codeFenceBuffer = "";
    const assistantId = `streaming-${Date.now()}`;
    const assistantMsg: AIMessage = {
      id: assistantId,
      sessionId: sid || "",
      role: "assistant",
      content: "",
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    set((s) => ({ messages: [...s.messages, assistantMsg] }));

    // 新一轮流式开始：清空上一轮的 Agent 工作台步骤条 + 流程元信息
    set({ agentSteps: [], agentMeta: null });

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1";
    const historySnapshot = get().messages.slice(-10).map((m) => ({ role: m.role, content: m.content }));
    const history = [...historySnapshot, { role: userMsg.role, content: userMsg.content }];

    const updateAssistant = (c: string) =>
      set((s) => ({ messages: s.messages.map((m) => (m.id === assistantId ? { ...m, content: c } : m)) }));

    /** 把一段 delta 累积进 visibleContent，自动识别 ``` 代码块并整块丢弃。 */
    const appendVisible = (delta: string): string => {
      let next = "";
      for (let i = 0; i < delta.length; i++) {
        const ch = delta[i];
        if (codeFenceState === "open") {
          codeFenceBuffer += ch;
          const last3 = codeFenceBuffer.slice(-3);
          if (last3.includes("```")) {
            codeFenceState = "closed";
            codeFenceBuffer = "";
            if (!next.endsWith("\n\n")) {
              if (next.endsWith("\n")) next += "\n";
              else next += "\n\n";
            }
          }
          continue;
        }
        if (ch === "`") {
          codeFenceBuffer += ch;
          if (codeFenceBuffer.length >= 3 && codeFenceBuffer.endsWith("```")) {
            if (next.length > 0 && !next.endsWith("\n\n") && !next.endsWith("\n")) next += "\n\n";
            codeFenceState = "open";
            codeFenceBuffer = "";
            continue;
          }
          if (codeFenceBuffer.length >= 3) {
            next += codeFenceBuffer;
            codeFenceBuffer = "";
          }
          continue;
        }
        if (codeFenceBuffer.length > 0) {
          next += codeFenceBuffer;
          codeFenceBuffer = "";
        }
        next += ch;
      }
      if (codeFenceState === "closed" && codeFenceBuffer.length > 0) {
        next += codeFenceBuffer;
        codeFenceBuffer = "";
      }
      visibleContent += next;
      return next;
    };

    try {
      const response = await fetch(`${baseUrl}/ai/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          datasource_id: selectedDsId || null,
          session_id: sid,
          message: content,
          history,
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        const msg = err?.detail?.message || err?.error?.message || `HTTP ${response.status}`;
        if (msg.includes("AI_NOT_CONFIGURED") || msg.includes("OPENAI_API_KEY")) {
          set({ aiNotConfigured: true });
        }
        updateAssistant(`[错误] ${msg}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let buffer = "";

      // SSE 行解析器（提取为可复用函数：循环内 + 循环结束后的 buffer 残留都要消费）
      const handleLine = (line: string) => {
        if (!line.startsWith("data: ")) return;
        const jsonStr = line.slice(6).trim();
        if (!jsonStr) return;
        let event: any;
        try { event = JSON.parse(jsonStr); } catch { return; }
        switch (event.type) {
          case "intent":
            set((s) => ({ agentMeta: { ...(s.agentMeta ?? {}), intent: event.intent, intentConfidence: event.confidence, degraded: event.degraded ?? s.agentMeta?.degraded } }));
            break;
          case "decision":
            set((s) => ({ agentMeta: { ...(s.agentMeta ?? {}), decision: event.action, decisionTool: event.tool, decisionReason: event.reason, degraded: event.degraded ?? s.agentMeta?.degraded } }));
            break;
          case "progress": {
            const pIdx = Number(event.index ?? 0);
            // 带 round 前缀，避免多轮循环下第二轮 index 覆盖第一轮步骤
            const pRound = Number(event.round ?? 0);
            const stepId = `p${pRound}_${pIdx}`;
            set((s) => {
              const base = {
                title: String(event.title ?? "执行中"),
                emphasis: event.status === "start",
              };
              const existing = s.agentSteps.find((st) => st.id === stepId);
              if (existing) {
                return { agentSteps: s.agentSteps.map((st) => (st.id === stepId ? { ...st, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: st.tools } : st)) };
              }
              return { agentSteps: [...s.agentSteps, { id: stepId, ...base, status: mapProgressStatus(String(event.status ?? "wait")), tools: [], expanded: false }] };
            });
            break;
          }
            case "session_created":
              if (event.session?.id) {
                const backendSid = event.session.id;
                set({ activeSessionId: backendSid });
                set((s) => {
                  const exists = s.sessions.some((ss) => ss.id === backendSid);
                  return { sessions: exists ? s.sessions : [event.session, ...s.sessions] };
                });
              }
              break;
            case "message":
              assistantContent += event.delta;
              appendVisible(event.delta);
              break;
            case "tool_call": {
              set((s) => {
                // 找到当前正在执行的 progress 步骤（最后一个 run 状态的 step）
                const runIdx = [...s.agentSteps].reverse().findIndex((st) => st.status === "run");
                if (runIdx === -1) return {};
                const idx = s.agentSteps.length - 1 - runIdx;
                const next = s.agentSteps.slice();
                next[idx] = { ...next[idx], tools: [...next[idx].tools, { name: event.name, args: event.args, status: "run" }] };
                return { agentSteps: next };
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
              set((s) => {
                const runIdx = [...s.agentSteps].reverse().findIndex((st) => st.tools.some((t) => t.status === "run"));
                if (runIdx === -1) return {};
                const idx = s.agentSteps.length - 1 - runIdx;
                return {
                  agentSteps: s.agentSteps.map((st, i) =>
                    i === idx
                      ? { ...st, tools: st.tools.map((t, j) => (j === st.tools.length - 1 ? { ...t, result: event.result, status: isErr ? "err" : "ok" } : t)) }
                      : st,
                  ),
                };
              });
              break;
            }
            case "query_error":
              visibleContent += `\n\n> ${event.message}`;
              break;
            case "error":
              visibleContent = `[错误] ${event.message}`;
              break;
            case "done": {
              const doneCharts: StreamingChart[] = event.charts || [];
              if (doneCharts.length > 0) {
                set((s) => ({
                  messages: s.messages.map((m) =>
                    m.id === assistantId ? { ...m, content: visibleContent, chartData: { charts: doneCharts } } : m,
                  ),
                }));
              }
              // done 收敛：工作台所有 step 置为完成（run → done / done 保持），避免残留"执行中"
              set((s) => ({
                agentSteps: s.agentSteps.map((st) =>
                  st.status === "run" || st.status === "wait"
                    ? { ...st, status: "done", tools: st.tools.map((t) => (t.status === "run" ? { ...t, status: "ok" } : t)) }
                    : st,
                ),
              }));
              agentStreamingLock = false;
              set({ isStreaming: false });
              break;
            }
            default:
              break;
          }
          if (event.type !== "done") {
            set((s) => ({
              messages: s.messages.map((m) => (m.id === assistantId ? { ...m, content: visibleContent } : m)),
            }));
          }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) handleLine(line);
      }

      // 循环结束后处理 buffer 残留（SSE 末尾可能无换行符，最后的 done/message 事件滞留在 buffer）
      if (buffer && buffer.trim()) {
        for (const line of buffer.split("\n")) handleLine(line);
      }
    } catch {
      // fetch error
    } finally {
      // 流中断/异常也收尾：不让工作台残留"执行中"状态
      set((s) => ({ agentSteps: s.agentSteps.map((st) => (st.status === "run" ? { ...st, status: "done", tools: st.tools } : st)) }));
      agentStreamingLock = false;
      set({ isStreaming: false });
    }
  },

  sendFallback: async (sid, content) => {
    if (fallbackStreamingLock) return;
    fallbackStreamingLock = true;
    set({ isStreaming: true });

    const userMsg: AIMessage = {
      id: `temp-${Date.now()}`,
      sessionId: sid,
      role: "user",
      content,
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    set((s) => ({ messages: [...s.messages, userMsg] }));

    let assistantContent = "";
    const assistantId = `streaming-${Date.now()}`;
    const assistantMsg: AIMessage = {
      id: assistantId,
      sessionId: sid,
      role: "assistant",
      content: "",
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    set((s) => ({ messages: [...s.messages, assistantMsg] }));

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1";

    const patchAssistant = (patch: Partial<Pick<AIMessage, "content" | "chartData">>) =>
      set((s) => ({ messages: s.messages.map((m) => (m.id === assistantId ? { ...m, ...patch } : m)) }));

    try {
      const response = await fetch(`${baseUrl}/ai/sessions/${sid}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ content }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        const msg = errData?.error?.message || errData?.detail?.message || `HTTP ${response.status}`;
        if (msg.includes("AI_NOT_CONFIGURED") || msg.includes("OPENAI_API_KEY")) {
          set({ aiNotConfigured: true });
        }
        patchAssistant({ content: `[错误] ${msg}` });
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;
          try {
            const event = JSON.parse(jsonStr);
            switch (event.type) {
              case "message":
                assistantContent += event.delta;
                patchAssistant({ content: assistantContent });
                break;
              case "chart":
                patchAssistant({ chartData: event.payload as Record<string, unknown> });
                break;
              case "done":
                // 后端已保存本轮消息：拉取一次真实数据刷新（含 auto-title）
                await get().loadMessages(sid);
                await get().refreshSessions();
                break;
              case "error":
                if (event.message?.includes("AI_NOT_CONFIGURED") || event.message?.includes("OPENAI_API_KEY")) {
                  set({ aiNotConfigured: true });
                }
                patchAssistant({ content: `[错误] ${event.message}` });
                break;
              default:
                break;
            }
          } catch { /* skip unparseable */ }
        }
      }

      // 循环结束后处理 buffer 残留（SSE 末尾可能无换行符，最后的 done 事件滞留在 buffer）
      if (buffer && buffer.trim()) {
        for (const line of buffer.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;
          try {
            const event = JSON.parse(jsonStr);
            if (event.type === "done") {
              await get().loadMessages(sid);
              await get().refreshSessions();
            }
          } catch { /* skip unparseable */ }
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "未知错误";
      patchAssistant({ content: `[连接失败] ${msg}` });
    } finally {
      fallbackStreamingLock = false;
      set({ isStreaming: false });
    }
  },

  sendChat: async (content, ctx) => {
    if (agentStreamingLock || fallbackStreamingLock) return;
    const trimmed = (content ?? "").trim();
    if (!trimmed) return;

    let sid = get().activeSessionId;
    if (!sid) {
      try {
        const session = await createSession("新对话");
        sid = session.id;
        set({ activeSessionId: sid, sessions: [session, ...get().sessions] });
      } catch {
        // 建会话失败则放弃发送
        return;
      }
    }

    if (ctx.selectedDsId || ctx.hasDatasources) {
      await get().streamChat(sid, trimmed, ctx.selectedDsId);
    } else {
      await get().sendFallback(sid, trimmed);
    }
  },

  clearStream: () => {
    agentStreamingLock = false;
    fallbackStreamingLock = false;
    set({ isStreaming: false });
  },
}));