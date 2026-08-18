import { useState, useEffect, useRef, useCallback } from "react";
import {
  Plus,
  Search,
  Sparkles,
  BarChart2,
  FileBarChart,
  Wand2,
  TrendingUp,
  Copy,
  ThumbsUp,
  ThumbsDown,
  Paperclip,
  ArrowUp,
  Trash2,
  Pencil,
} from "lucide-react";
import ChartCard from "./components/ChartCard";
import { useSSE } from "../../hooks/useSSE";
import {
  listSessions,
  createSession,
  listMessages,
  deleteSession,
  updateSession,
} from "../../api/ai";
import type { AISession, AIMessage } from "../../types/api";
import { listDatasources } from "../../api/datasources";
import { tokenStore } from "../../api/client";

/** 防御性兜底：去掉历史消息里残留的 ``` 代码块。流式阶段通常已丢，这里只处理从数据库读出来的旧消息。 */
function stripCodeBlocks(text: string): string {
  return text
    .replace(/```json[\s\S]*?```/g, "")
    .replace(/```sql[\s\S]*?```/g, "")
    .replace(/```[\s\S]*?```/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Simple markdown-to-JSX renderer for AI assistant messages */
function renderMarkdown(text: string): React.ReactNode[] {
  const lines = text.split("\n");
  const nodes: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Empty line → paragraph break
    if (!line.trim()) {
      nodes.push(<div key={i} className="h-2" />);
      i++;
      continue;
    }

    // ### Subtitle (colored, smaller)
    if (/^###\s/.test(line)) {
      nodes.push(
        <div key={i} className="text-[13px] font-semibold text-ai mt-2 mb-1">
          {line.replace(/^###\s+/, "")}
        </div>
      );
      i++;
      continue;
    }

    // ## Title (colored, prominent)
    if (/^##\s/.test(line)) {
      nodes.push(
        <div key={i} className="text-[14px] font-bold text-ai mt-3 mb-1.5 pb-1 border-b border-ai/20">
          {line.replace(/^##\s+/, "")}
        </div>
      );
      i++;
      continue;
    }

    // --- horizontal rule
    if (/^---+$/.test(line.trim())) {
      nodes.push(<div key={i} className="my-2 border-t border-border-light" />);
      i++;
      continue;
    }

    // > quote / callout (highlighted box)
    if (/^>\s/.test(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length && /^>\s/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      nodes.push(
        <div key={i - quoteLines.length} className="my-1.5 px-2.5 py-1.5 rounded-[6px] bg-ai-light/50 border-l-2 border-ai text-[12px] leading-relaxed">
          {quoteLines.map((ql, qi) => (
            <span key={qi}>{qi > 0 && <br />}{renderInline(ql)}</span>
          ))}
        </div>
      );
      continue;
    }

    // - bullet point
    if (/^-\s/.test(line)) {
      nodes.push(
        <div key={i} className="flex gap-1.5 pl-1 text-[12px] leading-relaxed">
          <span className="text-ai flex-shrink-0 mt-px">•</span>
          <span>{renderInline(line.replace(/^-\s+/, ""))}</span>
        </div>
      );
      i++;
      continue;
    }

    // | markdown table row
    if (/^\|/.test(line.trim())) {
      const cells = line.trim().split("|").filter(c => c.trim() !== "").map(c => c.trim());
      // If this is a separator row like |:---|---:| etc.
      if (cells.every(c => /^:?-{3,}:?$/.test(c))) {
        i++;
        continue;
      }
      // Push as a table row (card-style list item)
      nodes.push(
        <div key={i} className="flex gap-2 pl-1 text-[12px] leading-relaxed text-muted-foreground">
          {cells.map((c, ci) => (
            <span key={ci} className={ci === 0 ? "text-foreground font-medium" : ""}>{renderInline(c)}</span>
          ))}
        </div>
      );
      i++;
      continue;
    }

    // Numbered list 1. 2. etc
    if (/^\d+[.、]\s/.test(line)) {
      nodes.push(
        <div key={i} className="flex gap-1.5 pl-1 text-[12px] leading-relaxed">
          <span className="text-ai font-medium flex-shrink-0">{line.match(/^\d+/)?.[0]}.</span>
          <span>{renderInline(line.replace(/^\d+[.、]\s*/, ""))}</span>
        </div>
      );
      i++;
      continue;
    }

    // **bold title** on its own line → highlight label style
    if (/^\*\*.*\*\*$/.test(line.trim())) {
      const inner = line.trim().replace(/^\*\*(.*)\*\*$/, "$1");
      nodes.push(
        <div key={i} className="inline-block mt-2 mb-1 px-2 py-0.5 rounded-[4px] bg-ai-light text-ai text-[12px] font-semibold">
          {inner}
        </div>
      );
      i++;
      continue;
    }

    // Regular paragraph
    nodes.push(
      <div key={i} className="text-[12px] leading-relaxed">
        {renderInline(line)}
      </div>
    );
    i++;
  }

  return nodes;
}

/** Render inline bold and inline code within a single line */
function renderInline(text: string): React.ReactNode {
  const parts = text.split(/(\*\*.*?\*\*|`.*?`)/g);
  return parts.map((part, idx) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={idx} className="text-foreground font-semibold">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={idx} className="px-1 py-px rounded bg-border/50 text-[11px] font-mono">{part.slice(1, -1)}</code>;
    }
    return <span key={idx}>{part}</span>;
  });
}

const quickCards = [
  {
    icon: BarChart2,
    title: "分析数据",
    desc: "智能分析数据趋势",
    iconBg: "bg-primary-light",
    iconColor: "text-primary",
  },
  {
    icon: FileBarChart,
    title: "生成报表",
    desc: "一键生成专业报表",
    iconBg: "bg-ai-light",
    iconColor: "text-ai",
  },
  {
    icon: Wand2,
    title: "数据清洗",
    desc: "检测缺失值、异常值、重复数据并给出清洗建议",
    iconBg: "bg-warning-light",
    iconColor: "text-warning",
  },
  {
    icon: TrendingUp,
    title: "趋势预测",
    desc: "预测未来数据走势",
    iconBg: "bg-success-light",
    iconColor: "text-success",
  },
];

function relativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins}分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}天前`;
  return new Date(dateStr).toLocaleDateString("zh-CN");
}

export default function AIChat() {
  const [sessions, setSessions] = useState<AISession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [aiNotConfigured, setAiNotConfigured] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [datasourceList, setDatasourceList] = useState<Array<{id: string; name: string}>>([]);
  const [selectedDsId, setSelectedDsId] = useState<string>('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const prevSessionRef = useRef<string | null>(null);
  const isAgentStreamingRef = useRef(false); // P1: 防并发
  const selectedDsIdRef = useRef(selectedDsId); // P5: 防闭包陈旧
  const messagesRef = useRef<AIMessage[]>([]); // 修复: streamDataChat 闭包陈旧导致新会话发送失败

  // Agent streaming state
  interface StreamingChart {
    chart_type: string;
    option: Record<string, unknown>;
  }

  const { sendMessage, isStreaming } = useSSE();

  const fetchSessions = useCallback(async () => {
    try {
      setSessionsLoading(true);
      const list = await listSessions();
      setSessions(list);
    } catch {
      // silently fail
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const fetchMessages = useCallback(async (sid: string) => {
    try {
      setMessagesLoading(true);
      const msgs = await listMessages(sid);
      setMessages(msgs);
    } catch {
      // silently fail
    } finally {
      setMessagesLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  useEffect(() => {
    listDatasources({ pageSize: 100 }).then((res) => {
      setDatasourceList((res.items || []).map((ds) => ({ id: ds.id, name: ds.name })));
    }).catch(() => {});
  }, []);

  // P5: 保持 ref 与 state 同步
  useEffect(() => { selectedDsIdRef.current = selectedDsId; }, [selectedDsId]);
  useEffect(() => { messagesRef.current = messages; }, [messages]); // 修复闭包陈旧

  useEffect(() => {
    if (activeSessionId) {
      if (prevSessionRef.current !== activeSessionId) {
        fetchMessages(activeSessionId);
        prevSessionRef.current = activeSessionId;
      }
    } else {
      prevSessionRef.current = null;
    }
  }, [activeSessionId, fetchMessages]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    // Use rAF to batch DOM reads/writes and prevent visual bounce
    requestAnimationFrame(() => {
      el.style.height = "auto";
      el.style.height = Math.max(40, Math.min(el.scrollHeight, 120)) + "px";
    });
  }, [inputValue]);

  const handleNewSession = async () => {
    try {
      const session = await createSession("新对话");
      setSessions((prev) => [session, ...prev]);
      setActiveSessionId(session.id);
      setMessages([]);
    } catch {
      // silently fail
    }
  };

  const handleDeleteSession = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeSessionId === id) {
        setActiveSessionId(null);
        setMessages([]);
      }
    } catch {
      // silently fail
    }
  };

  const startRename = (id: string, currentTitle: string) => {
    setEditingSessionId(id);
    setEditingTitle(currentTitle || "");
  };

  const submitRename = async (id: string) => {
    const newTitle = editingTitle.trim();
    if (newTitle && newTitle !== sessions.find(s => s.id === id)?.title) {
      try {
        await updateSession(id, newTitle);
        setSessions(prev => prev.map(s => s.id === id ? { ...s, title: newTitle } : s));
      } catch { /* ignore */ }
    }
    setEditingSessionId(null);
    setEditingTitle("");
  };

  const handleQuickCard = (title: string, desc: string) => {
    if (title === "数据清洗") {
      setInputValue("请分析当前数据源的数据质量，检测缺失值、异常值、重复行等问题，给出清洗建议");
    } else {
      setInputValue(`${title}：${desc}`);
    }
    textareaRef.current?.focus();
  };

  /** Reusable SSE streaming for data chat */
  const streamDataChat = async (
    sid: string | null,
    content: string,
  ) => {
    // P1: 防并发 - 如果已有流在执行中，直接返回
    if (isAgentStreamingRef.current) return;
    isAgentStreamingRef.current = true;

    // P5: 使用 ref 避免闭包陈旧
    const currentDsId = selectedDsIdRef.current;

    const userMsg: AIMessage = {
      id: `temp-${Date.now()}`,
      sessionId: sid || '',
      role: "user",
      content,
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    let assistantContent = "";
    let visibleContent = "";
    let codeFenceState: "open" | "closed" = "closed";
    let codeFenceBuffer = "";
    const assistantId = `streaming-${Date.now()}`;
    const assistantMsg: AIMessage = {
      id: assistantId,
      sessionId: sid || '',
      role: "assistant",
      content: "",
      chartData: null,
      createdAt: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, assistantMsg]);

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1';
    // 使用 ref 避免闭包陈旧，获取最新的 messages
    const historySnapshot = messagesRef.current.slice(-10).map(m => ({ role: m.role, content: m.content }));
    const history = [...historySnapshot, { role: userMsg.role, content: userMsg.content }];

    /** 把一段 delta 累积进 visibleContent，自动识别 ``` 代码块并整块丢弃。 */
    const appendVisible = (delta: string): string => {
      let next = "";
      for (let i = 0; i < delta.length; i++) {
        const ch = delta[i];
        if (codeFenceState === "open") {
          if (ch === "\n") {
            codeFenceBuffer += ch;
          } else {
            codeFenceBuffer += ch;
          }
          // 检测闭合
          const last3 = codeFenceBuffer.slice(-3);
          if (last3.includes("```")) {
            codeFenceState = "closed";
            codeFenceBuffer = "";
            // 代码块闭合后强制加一个段落分隔，避免前后两段文本粘在一起
            if (!next.endsWith("\n\n")) {
              if (next.endsWith("\n")) {
                next += "\n";
              } else {
                next += "\n\n";
              }
            }
          }
          continue;
        }
        // 在 closed 状态：识别开 fence
        if (ch === "`") {
          // 收集一段看是否为 ```
          codeFenceBuffer += ch;
          if (codeFenceBuffer.length >= 3 && codeFenceBuffer.endsWith("```")) {
            // 进入 open 前确保前面有段落分隔
            if (next.length > 0 && !next.endsWith("\n\n") && !next.endsWith("\n")) {
              next += "\n\n";
            }
            codeFenceState = "open";
            codeFenceBuffer = "";
            continue;
          }
          // 还没到 3 个 backtick：暂存到 buffer，不输出
          if (codeFenceBuffer.length >= 3) {
            // 超过 3 个：视为普通文本冲刷 buffer
            next += codeFenceBuffer;
            codeFenceBuffer = "";
          }
          continue;
        }
        // 普通字符：先 flush buffer
        if (codeFenceBuffer.length > 0) {
          next += codeFenceBuffer;
          codeFenceBuffer = "";
        }
        next += ch;
      }
      // 末尾 buffer 残留（可能是不闭合的 ``` 起始）：暂存不输出，避免后续拼到 closing 时混淆
      if (codeFenceState === "closed" && codeFenceBuffer.length > 0) {
        next += codeFenceBuffer;
        codeFenceBuffer = "";
      }
      visibleContent += next;
      return next;
    };

    // P2: 收集图表数据，流结束后存入消息
    let collectedCharts: StreamingChart[] = [];

    try {
      const response = await fetch(`${baseUrl}/ai/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({
          datasource_id: currentDsId || null,
          session_id: sid,
          message: content,
          history,
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        const msg = err?.detail?.message || err?.error?.message || `HTTP ${response.status}`;
        if (msg.includes("AI_NOT_CONFIGURED") || msg.includes("OPENAI_API_KEY")) {
          setAiNotConfigured(true);
        }
        setMessages((prev) =>
          prev.map((m) => m.id === assistantId ? { ...m, content: `[错误] ${msg}` } : m)
        );
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;
          try {
            const event = JSON.parse(jsonStr);
            switch (event.type) {
              case 'session_created':
                if (event.session?.id) {
                  const backendSid = event.session.id;
                  if (sid !== backendSid) {
                    sid = backendSid;
                    setActiveSessionId(backendSid);
                    setSessions((prev) => {
                      const exists = prev.some(s => s.id === backendSid);
                      return exists ? prev : [event.session, ...prev];
                    });
                  }
                }
                break;
              case 'message':
                assistantContent += event.delta;
                appendVisible(event.delta);
                break;
              case 'query_error':
                visibleContent += `\n\n> ${event.message}`;
                break;
              case 'error':
                visibleContent = `[错误] ${event.message}`;
                break;
              case 'done':
                // 从 done 事件中提取批量图表（后端已缓存所有图表，一次性发送）
                const doneCharts: StreamingChart[] = event.charts || [];
                if (doneCharts.length > 0) {
                  collectedCharts = doneCharts;
                  setMessages((prev) =>
                    prev.map((m) =>
                      m.id === assistantId
                        ? { ...m, content: visibleContent, chartData: { charts: doneCharts } }
                        : m
                    )
                  );
                }
                break;
            }
            if (event.type !== 'done') {
              setMessages((prev) =>
                prev.map((m) => m.id === assistantId ? { ...m, content: visibleContent } : m)
              );
            }
          } catch { /* skip unparseable */ }
        }
      }
    } catch {
      // fetch error
    } finally {
      isAgentStreamingRef.current = false;
    }
  };

  const handleSend = async () => {
    if (!inputValue.trim() || isStreaming || isAgentStreamingRef.current) return;

    const content = inputValue.trim();
    setInputValue("");

    // Reset agent streaming state

    try {
      let sid = activeSessionId;

      // Create session if needed
      if (!sid) {
        const session = await createSession("新对话");
        sid = session.id;
        setActiveSessionId(sid);
        setSessions((prev) => [session, ...prev]);
      }

      // If datasource available, use agent mode
      if (selectedDsId || datasourceList.length > 0) {
        await streamDataChat(sid, content);
        return;
      }

      // Fallback: regular chat (only when no data sources exist)
      const userMsg: AIMessage = {
        id: `temp-${Date.now()}`,
        sessionId: sid,
        role: "user",
        content,
        chartData: null,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg]);

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
      setMessages((prev) => [...prev, assistantMsg]);

      sendMessage(sid, content, {
        onDelta: (delta) => {
          assistantContent += delta;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, content: assistantContent } : m
            )
          );
        },
        onChart: (payload) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? { ...m, chartData: payload as Record<string, unknown> }
                : m
            )
          );
        },
        onDone: () => {
          if (sid) fetchMessages(sid);
          if (
            sessions.find((s) => s.id === sid && (s.title === "新对话" || !s.title))
          ) {
            fetchSessions();
          }
        },
        onError: (msg) => {
          if (
            msg.includes("AI_NOT_CONFIGURED") ||
            msg.includes("OPENAI_API_KEY")
          ) {
            setAiNotConfigured(true);
          }
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? { ...m, content: `[错误] ${msg}` }
                : m
            )
          );
        },
      });
    } catch {
      // handle fetch errors silently
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const filteredSessions = searchQuery
    ? sessions.filter((s) =>
        (s.title || "").toLowerCase().includes(searchQuery.toLowerCase())
      )
    : sessions;

  const hasMessages = messages.length > 0;

  return (
    <div className="h-screen flex overflow-hidden">
      {/* Left Sidebar */}
      <div className="w-[260px] flex-shrink-0 flex flex-col border-r border-border-light bg-background">
        <div className="flex items-center justify-between px-4 py-4">
          <h2 className="text-[14px] font-semibold text-foreground">
            对话历史
          </h2>
          <button
            onClick={handleNewSession}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--radius-sm)] bg-primary text-white text-[12px] font-medium hover:bg-primary-hover transition-colors shadow-sm"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>新建对话</span>
          </button>
        </div>

        <div className="px-4 pb-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
            <input
              type="text"
              placeholder="搜索对话..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-[var(--radius-sm)] bg-card border border-border text-[12.5px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring focus:border-ring transition-shadow"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-3 space-y-0.5">
          {sessionsLoading ? (
            <div className="px-3 py-6 text-center text-[12px] text-muted-foreground">
              加载中...
            </div>
          ) : filteredSessions.length === 0 ? (
            <div className="px-3 py-6 text-center text-[12px] text-muted-foreground">
              {searchQuery ? "未找到匹配的对话" : "暂无对话，点击新建开始吧"}
            </div>
          ) : (
            filteredSessions.map((session) => {
              const isActive = session.id === activeSessionId;
              const isEditing = session.id === editingSessionId;
              return (
                <div
                  key={session.id}
                  onClick={() => isEditing ? null : setActiveSessionId(session.id)}
                  onDoubleClick={() => startRename(session.id, session.title || "")}
                  className={`group px-3 py-3 rounded-[var(--radius-md)] cursor-pointer transition-colors flex items-center justify-between ${
                    isActive
                      ? "bg-card shadow-sm border border-border-light"
                      : "hover:bg-card"
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    {isEditing ? (
                      <input
                        autoFocus
                        value={editingTitle}
                        onChange={(e) => setEditingTitle(e.target.value)}
                        onBlur={() => submitRename(session.id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") submitRename(session.id);
                          if (e.key === "Escape") { setEditingSessionId(null); setEditingTitle(""); }
                        }}
                        onClick={(e) => e.stopPropagation()}
                        className="w-full text-[13px] font-medium px-1.5 py-0.5 rounded border border-primary bg-white outline-none"
                      />
                    ) : (
                      <p
                        className={`text-[13px] truncate ${
                          isActive
                            ? "font-medium text-foreground"
                            : "text-card-foreground"
                        }`}
                      >
                        {session.title || "新对话"}
                      </p>
                    )}
                    <p className="text-[11px] text-muted-foreground mt-1">
                      {relativeTime(session.createdAt)}
                    </p>
                  </div>
                  <div className="flex items-center gap-0.5 ml-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); startRename(session.id, session.title || ""); }}
                      className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-muted text-muted-foreground hover:text-primary transition-all flex-shrink-0"
                      title="重命名"
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                    <button
                      onClick={(e) => handleDeleteSession(session.id, e)}
                      className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-muted text-muted-foreground hover:text-destructive transition-all flex-shrink-0"
                      title="删除对话"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-background">
        {/* AI Not Configured Banner */}
        {aiNotConfigured && (
          <div className="px-6 py-2.5 bg-warning-light border-b border-warning-muted flex items-center gap-2.5 text-[13px]">
            <span className="text-base">&#9888;&#65039;</span>
            <span className="text-foreground">
              DeepSeek API Key 未配置，请在 backend/.env 中设置 OPENAI_API_KEY
            </span>
          </div>
        )}

        <div className="flex items-center justify-between px-6 py-3.5 border-b border-border-light bg-card">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-lg bg-ai-light flex items-center justify-center">
              <Sparkles className="w-3.5 h-3.5 text-ai" />
            </div>
            <span className="text-[14px] font-semibold text-foreground">
              Lvco AI 助手
            </span>
            <span
              className={`w-2 h-2 rounded-full ${(isStreaming || isAgentStreamingRef.current) ? "bg-ai animate-pulse" : "bg-success"}`}
            />
          </div>
          <div className="flex items-center gap-3">
            <span className="text-[12px] text-muted-foreground bg-muted border border-border rounded-[var(--radius-sm)] px-3 py-1.5">
              DeepSeek
            </span>
          </div>
        </div>

        {datasourceList.length > 0 && (
          <div className="px-4 py-2 border-b border-border-light flex items-center gap-2">
            <span className="text-[12px] text-muted-foreground">数据源：</span>
            <select
              value={selectedDsId}
              onChange={e => setSelectedDsId(e.target.value)}
              className="px-2 py-1 text-[12px] rounded border border-border bg-white"
            >
              <option value="">通用对话（不绑定数据）</option>
              {datasourceList.map(ds => (
                <option key={ds.id} value={ds.id}>{ds.name}</option>
              ))}
            </select>
            {selectedDsId && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-success-light text-success">数据已就绪</span>
            )}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-6 py-6 space-y-6">
          {messagesLoading ? (
            <div className="text-center py-12">
              <p className="text-[13px] text-muted-foreground">加载消息中...</p>
            </div>
          ) : !hasMessages ? (
            <>
              {/* Welcome Section */}
              <div className="text-center py-8">
                <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-ai-light to-primary-light flex items-center justify-center mx-auto mb-4">
                  <Sparkles className="w-7 h-7 text-ai" />
                </div>
                <h1 className="text-[20px] font-semibold text-foreground mb-2">
                  你好，我是 Lvco AI 助手
                </h1>
                <p className="text-[13.5px] text-muted-foreground">
                  我可以帮你分析数据、生成报表、洞察趋势
                </p>
              </div>

              {/* Quick Cards */}
              <div className="grid grid-cols-2 gap-3 max-w-lg mx-auto">
                {quickCards.map((card) => (
                  <div
                    key={card.title}
                    onClick={() => handleQuickCard(card.title, card.desc)}
                    className="flex items-center gap-3 px-4 py-3.5 rounded-[var(--radius-md)] bg-card border border-border-light shadow-sm hover:shadow-md hover:border-primary-muted cursor-pointer transition-all"
                  >
                    <div
                      className={`w-9 h-9 rounded-lg ${card.iconBg} flex items-center justify-center flex-shrink-0`}
                    >
                      <card.icon className={`w-4 h-4 ${card.iconColor}`} />
                    </div>
                    <div>
                      <p className="text-[13px] font-medium text-foreground">
                        {card.title}
                      </p>
                      <p className="text-[11px] text-muted-foreground">
                        {card.desc}
                      </p>
                    </div>
                  </div>
                ))}
              </div>

              {/* Empty Divider */}
              <div className="flex items-center gap-3 py-2">
                <div className="flex-1 h-px bg-border-light" />
                <span className="text-[11px] text-muted-foreground">
                  开始对话
                </span>
                <div className="flex-1 h-px bg-border-light" />
              </div>
            </>
          ) : (
            /* Messages */
            messages.map((msg) => {
              const isUser = msg.role === "user";
              return (
                <div
                  key={msg.id}
                  className={`flex ${isUser ? "justify-end" : "gap-3 max-w-[80%]"}`}
                >
                  {!isUser && (
                    <div className="w-7 h-7 rounded-lg bg-ai-light flex items-center justify-center flex-shrink-0 mt-0.5">
                      <Sparkles className="w-3.5 h-3.5 text-ai" />
                    </div>
                  )}
                  <div
                    className={
                      isUser
                        ? "max-w-[70%] px-4 py-3 rounded-[var(--radius-md)] rounded-tr-sm bg-muted text-[13px] text-card-foreground leading-relaxed"
                        : "flex-1 min-w-0"
                    }
                  >
                    {isUser ? (
                      <p className="text-[13px] text-card-foreground leading-relaxed whitespace-pre-wrap">
                        {msg.content}
                      </p>
                    ) : (
                      <div className="px-4 py-3 rounded-[var(--radius-md)] rounded-tl-sm bg-card shadow-[var(--shadow-card)] border border-border-light">
                        {msg.content ? (
                          <div>
                            {renderMarkdown(stripCodeBlocks(msg.content))}
                          </div>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[13px] text-muted-foreground">
                            <span className="animate-pulse">●</span>
                            思考中...
                          </span>
                        )}
                        {/* Chart cards：从 chartData 批量渲染，done 事件一次性发送所有图表 */}
                        {(() => {
                          const charts = (msg.chartData as Record<string, unknown> | null)?.charts as StreamingChart[] | undefined;
                          if (!charts || charts.length === 0) return null;
                          return (
                            <div className="space-y-3 mt-2">
                              {charts.map((cc, i) => (
                                <ChartCard key={`${msg.id}-${i}`} chartType={cc.chart_type} option={cc.option} />
                              ))}
                            </div>
                          );
                        })()}
                      </div>
                    )}
                    {!isUser && msg.content && !msg.id.startsWith("streaming-") && (
                      <div className="flex items-center gap-2 mt-2 ml-1">
                        <button className="text-muted-foreground hover:text-primary transition-colors">
                          <Copy className="w-3.5 h-3.5" />
                        </button>
                        <button className="text-muted-foreground hover:text-primary transition-colors">
                          <ThumbsUp className="w-3.5 h-3.5" />
                        </button>
                        <button className="text-muted-foreground hover:text-primary transition-colors">
                          <ThumbsDown className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="flex-shrink-0 px-6 pb-5 pt-2">
          <div className="flex items-center gap-2 mb-3 flex-nowrap overflow-hidden">
            <span className="text-[11px] text-muted-foreground flex-shrink-0">快捷：</span>
            <button
              onClick={() => setInputValue("请帮我生成一个数据可视化图表")}
              className="px-2.5 py-1 rounded-full text-[11px] border border-border text-card-foreground hover:border-primary hover:text-primary hover:bg-primary-light transition-colors"
            >
              生成图表
            </button>
            <button
              onClick={() => setInputValue("请帮我分析这份数据的主要特征和趋势")}
              className="px-2.5 py-1 rounded-full text-[11px] border border-border text-card-foreground hover:border-primary hover:text-primary hover:bg-primary-light transition-colors"
            >
              数据分析
            </button>
            <button
              onClick={() => setInputValue("请帮我根据数据生成一份分析报表")}
              className="px-2.5 py-1 rounded-full text-[11px] border border-border text-card-foreground hover:border-primary hover:text-primary hover:bg-primary-light transition-colors"
            >
              导出报表
            </button>
          </div>

          <div className="flex items-end gap-3 px-4 py-3 rounded-[var(--radius-md)] bg-card border border-border shadow-[var(--shadow-sm)] focus-within:border-ring focus-within:shadow-[0_0_0_2px_rgba(43,181,160,0.1)] transition-all">
            <button className="text-muted-foreground hover:text-primary transition-colors pb-0.5">
              <Paperclip className="w-4 h-4" />
            </button>
            <textarea
              ref={textareaRef}
              rows={1}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={aiNotConfigured}
              placeholder={
                aiNotConfigured
                  ? "请先在 backend/.env 中配置 DeepSeek API Key"
                  : "输入你的问题，例如：分析本季度各区域的销售表现..."
              }
              className="flex-1 text-[13px] text-foreground placeholder:text-muted-foreground bg-transparent border-none outline-none resize-none leading-relaxed transition-[height] duration-150 ease-out"
            />
            <button
              onClick={handleSend}
              disabled={!inputValue.trim() || isStreaming || isAgentStreamingRef.current || aiNotConfigured}
              className="w-8 h-8 rounded-full bg-ai hover:bg-ai-hover flex items-center justify-center transition-colors flex-shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ArrowUp className="w-4 h-4 text-white" />
            </button>
          </div>
          <p className="text-[10.5px] text-muted-foreground text-center mt-2">
            AI 生成内容仅供参考，请结合实际情况判断
          </p>
        </div>
      </div>
    </div>
  );
}
