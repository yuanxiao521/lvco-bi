import { useState, useRef, useEffect, useCallback, memo } from "react";
import { MessageCircle, Sparkles, X, Send, Loader2, GripVertical } from "lucide-react";
import { tokenStore } from "../../../api/client";

interface AIAssistantProps {
  datasourceId: string | null;
  fieldMeta: Array<{ name: string; data_type: string; category?: string }> | null;
  currentDimensions?: string[];
  currentMeasures?: Array<{ field: string; agg: string }>;
  currentChartType?: string;
  allDatasources?: Array<{ id: string; name: string; fields?: Array<{name: string; data_type: string}> }>;
  onApplyChartConfig?: (config: { chartType?: string; dimensions?: string[]; measures?: Array<{ field: string; agg: string }> }) => void;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

/** Strip ```json and ```sql code blocks from display content */
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


export default memo(function AIAssistant({
  datasourceId,
  fieldMeta,
  currentDimensions,
  currentMeasures,
  currentChartType,
  allDatasources,
  onApplyChartConfig,
}: AIAssistantProps) {
  const [open, setOpen] = useState(false);

  // Ball drag state
  const [ballPos, setBallPos] = useState<{ x: number; y: number } | null>(null);
  const [isDraggingBall, setIsDraggingBall] = useState(false);
  const ballDragStart = useRef({ x: 0, y: 0 });
  const ballPosStart = useRef({ x: 0, y: 0 });
  const ballDragMoved = useRef(false);

  // Panel drag state
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const dragStart = useRef({ x: 0, y: 0 });
  const posStart = useRef({ x: 0, y: 0 });

  // Resize state
  const [size, setSize] = useState({ w: 380, h: 480 });
  const [isResizing, setIsResizing] = useState(false);
  const resizeStart = useRef({ x: 0, y: 0, w: 0, h: 0 });

  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: datasourceId && fieldMeta?.length
        ? `你好！我已了解当前数据源，共有 ${fieldMeta.length} 个字段。你可以让我帮你分析数据、推荐图表。`
        : "你好！我是 AI 画布助手。先选择数据源，我就能帮你分析数据和配置图表。",
    },
  ]);
  const [inputValue, setInputValue] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Ball drag handler
  const onBallMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingBall(true);
    ballDragMoved.current = false;
    ballDragStart.current = { x: e.clientX, y: e.clientY };
    const currentPos = ballPos ?? { x: window.innerWidth - 72, y: window.innerHeight - 72 };
    ballPosStart.current = currentPos;
  }, [ballPos]);

  // Drag handlers
  const onTitleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    dragStart.current = { x: e.clientX, y: e.clientY };
    posStart.current = { x: pos.x, y: pos.y };
  }, [pos]);

  // Resize handler
  const onResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsResizing(true);
    resizeStart.current = { x: e.clientX, y: e.clientY, w: size.w, h: size.h };
  }, [size]);

  useEffect(() => {
    if (!isDraggingBall && !isDragging && !isResizing) return;

    const onMove = (e: MouseEvent) => {
      if (isDraggingBall) {
        const dx = e.clientX - ballDragStart.current.x;
        const dy = e.clientY - ballDragStart.current.y;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
          ballDragMoved.current = true;
        }
        setBallPos({
          x: Math.max(0, Math.min(window.innerWidth - 48, ballPosStart.current.x + dx)),
          y: Math.max(0, Math.min(window.innerHeight - 48, ballPosStart.current.y + dy)),
        });
      }
      if (isDragging) {
        setPos({
          x: posStart.current.x + (e.clientX - dragStart.current.x),
          y: posStart.current.y + (e.clientY - dragStart.current.y),
        });
      }
      if (isResizing) {
        const dw = e.clientX - resizeStart.current.x;
        const dh = e.clientY - resizeStart.current.y;
        setSize({
          w: Math.max(320, Math.min(700, resizeStart.current.w + dw)),
          h: Math.max(320, Math.min(800, resizeStart.current.h + dh)),
        });
      }
    };
    const onUp = () => { setIsDraggingBall(false); setIsDragging(false); setIsResizing(false); };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [isDraggingBall, isDragging, isResizing]);

  // 数据源变化时更新问候语
  useEffect(() => {
    if (datasourceId && fieldMeta?.length) {
      setMessages((prev) =>
        prev[0]?.id === "welcome"
          ? [
              {
                id: "welcome",
                role: "assistant",
                content: `当前数据源有 ${fieldMeta.length} 个字段，包括：${fieldMeta
                  .slice(0, 8)
                  .map((f) => f.name)
                  .join("、")}${fieldMeta.length > 8 ? "等" : ""}。\n\n- 推荐适合的图表类型\n- 分析数据分布\n- 查找数据规律`,
              },
              ...prev.slice(1),
            ]
          : prev
      );
    } else if (allDatasources?.length) {
      const dsNames = allDatasources.map(d => d.name).join('、');
      setMessages((prev) =>
        prev[0]?.id === "welcome"
          ? [
              {
                id: "welcome",
                role: "assistant",
                content: `你好！当前有以下数据源可用：${dsNames}。\n\n请选择一个数据源开始分析，或直接告诉我你想分析什么数据。`,
              },
              ...prev.slice(1),
            ]
          : prev
      );
    }
  }, [datasourceId, fieldMeta, allDatasources]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = async (text?: string) => {
    const content = (text || inputValue).trim();
    if (!content || isStreaming) return;
    if (!datasourceId) {
      setMessages(prev => [...prev, { id: `e-${Date.now()}`, role: "assistant", content: "请先在左侧选择一个数据源" }]);
      return;
    }
    setInputValue("");

    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: "user", content };
    setMessages(prev => [...prev, userMsg]);

    const assistantId = `a-${Date.now()}`;
    const assistantMsg: ChatMessage = { id: assistantId, role: "assistant", content: "" };
    setMessages(prev => [...prev, assistantMsg]);

    let assistantContent = "";
    setIsStreaming(true);

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1';

    const validFieldNames = new Set((fieldMeta ?? []).map(f => f.name).concat((fieldMeta ?? []).map(f => f.name.toLowerCase())));
    const cleanDims = (currentDimensions ?? []).filter(
      d => validFieldNames.has(d) || validFieldNames.has(d.toLowerCase())
    );
    const cleanMeasures = (currentMeasures ?? []).filter(
      m => validFieldNames.has(m.field) || validFieldNames.has(m.field.toLowerCase())
    );
    const hasValidCurrentConfig = cleanDims.length > 0 || cleanMeasures.length > 0;

    // 即使没有选中图表，也传递完整字段列表让 AI 可以智能推荐
    const canvasContext: Record<string, unknown> = {};
    if (hasValidCurrentConfig) {
      canvasContext.currentConfig = { dimensions: cleanDims, measures: cleanMeasures, chartType: currentChartType };
    }
    // Always provide full field list so AI can suggest charts without selection
    canvasContext.availableFields = (fieldMeta ?? []).map(f => ({ name: f.name, data_type: f.data_type, category: f.category }));

    try {
      const response = await fetch(`${baseUrl}/ai/canvas/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({
          datasource_id: datasourceId,
          session_id: 'canvas-session',
          message: content,
          canvas_context: canvasContext,
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err?.detail?.message || err?.error?.message || `HTTP ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('No response body');

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
              case 'message':
                assistantContent += event.delta;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              case 'query_result': {
                // Don't show raw data table in chat — AI will use it for chart configs
                // Only append a minimal indicator
                assistantContent += '';
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              }
              case 'query_error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              case 'chart_config':
                // Don't show raw JSON in chat — show a styled badge
                assistantContent += `\n\n[图表配置已应用]`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                if (onApplyChartConfig && event.config) {
                  onApplyChartConfig(event.config);
                }
                break;
              case 'chart_config_error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              case 'error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
            }
          } catch { /* skip */ }
        }
      }
    } catch (err: any) {
      setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: `[连接失败] ${err.message}` } : m));
    } finally {
      setIsStreaming(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Panel style based on drag + resize state
  const panelStyle: React.CSSProperties = {
    width: size.w,
    height: size.h,
    transform: `translate(${pos.x}px, ${pos.y}px)`,
    cursor: isDragging ? "grabbing" : undefined,
    userSelect: isDragging || isResizing ? "none" : undefined,
  };

  return (
    <div className="z-50" style={ballPos ? { position: 'fixed', left: ballPos.x, top: ballPos.y } : { position: 'fixed', bottom: 24, right: 24 }}>
      {open && (
        <div
          ref={panelRef}
          className="absolute bottom-14 right-0 bg-white rounded-[14px] overflow-hidden flex flex-col shadow-float border border-border-light"
          style={panelStyle}
        >
          {/* 标题栏 — draggable handle */}
          <div
            className="flex items-center justify-between px-3 py-2.5 border-b border-border-light bg-card cursor-grab select-none"
            onMouseDown={onTitleMouseDown}
          >
            <div className="flex items-center gap-2">
              <GripVertical className="w-3.5 h-3.5 text-muted-foreground/50" />
              <div className="w-6 h-6 rounded-full flex items-center justify-center bg-ai-light">
                <Sparkles className="w-3.5 h-3.5 text-ai" />
              </div>
              <span className="text-[13px] font-semibold text-foreground">AI 画布助手</span>
              {datasourceId && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-success-light text-success">
                  已就绪
                </span>
              )}
            </div>
            <button className="p-1 rounded hover:bg-muted" onClick={() => setOpen(false)}>
              <X className="w-4 h-4 text-muted-foreground" />
            </button>
          </div>

          {/* 消息列表 */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex gap-2 ${msg.role === "user" ? "justify-end" : ""}`}
              >
                {msg.role === "assistant" && (
                  <div className="w-6 h-6 rounded-full flex-shrink-0 flex items-center justify-center bg-ai-light mt-0.5">
                    <Sparkles className="w-3 h-3 text-ai" />
                  </div>
                )}
                <div
                  className={`text-[12.5px] leading-relaxed px-3 py-2 rounded-[10px] max-w-[90%] ${
                    msg.role === "user"
                      ? "bg-primary text-white rounded-tr-[2px]"
                      : "bg-muted text-card-foreground rounded-tl-[2px]"
                  }`}
                >
                  {msg.content ? (
                    msg.role === "assistant" && msg.id !== "welcome"
                      ? <div>{renderMarkdown(stripCodeBlocks(msg.content))}</div>
                      : <span className="whitespace-pre-wrap">{msg.content}</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-muted-foreground">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      思考中...
                    </span>
                  )}
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* 快捷操作 */}
          {datasourceId && (
            <div className="px-4 pb-1 flex gap-1.5 flex-wrap">
              {[
                { label: "推荐图表", text: "根据当前数据源字段，推荐最适合的图表类型" },
                { label: "数据概览", text: "帮我简要概述这个数据集的主要特征" },
                { label: "TOP5", text: "帮我找出数据中的TOP5关键指标" },
              ].map((btn) => (
                <button
                  key={btn.label}
                  onClick={() => handleSend(btn.text)}
                  disabled={isStreaming}
                  className="px-2 py-1 rounded-full text-[11px] border border-border text-muted-foreground hover:border-ai hover:text-ai hover:bg-ai-light transition-colors disabled:opacity-50"
                >
                  {btn.label}
                </button>
              ))}
            </div>
          )}

          {/* 输入区 */}
          <div className="px-3 py-2.5 border-t border-border-light">
            <div className="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-border bg-background">
              <input
                ref={inputRef}
                type="text"
                placeholder={
                  datasourceId
                    ? "输入你的问题，如：推荐图表..."
                    : "请先在左侧选择数据源"
                }
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isStreaming}
                className="flex-1 text-[12.5px] outline-none bg-transparent text-foreground placeholder:text-muted-foreground"
              />
              <button
                onClick={() => handleSend()}
                disabled={!inputValue.trim() || isStreaming}
                className="p-1 rounded text-ai hover:bg-ai-light transition-colors disabled:opacity-50"
              >
                {isStreaming ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>

          {/* 缩放手柄 — 右下角 */}
          <div
            className="absolute right-0 bottom-0 w-4 h-4 cursor-nwse-resize"
            onMouseDown={onResizeMouseDown}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" className="absolute right-0.5 bottom-0.5 text-muted-foreground/40">
              <path d="M0 12 L12 0" stroke="currentColor" strokeWidth="1.5" />
              <path d="M4 12 L12 4" stroke="currentColor" strokeWidth="1.5" />
              <path d="M8 12 L12 8" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </div>
        </div>
      )}

      {/* 悬浮按钮 */}
      <button
        className={`w-12 h-12 rounded-full flex items-center justify-center text-white bg-ai hover:bg-ai-hover shadow-float transition-all duration-200 hover:scale-110 active:scale-90 select-none ${isDraggingBall ? 'cursor-grabbing' : 'cursor-grab'}`}
        onMouseDown={onBallMouseDown}
        onClick={() => { if (!ballDragMoved.current) setOpen((v) => !v); }}
        title="AI 画布助手"
      >
        <MessageCircle className="w-5 h-5" />
      </button>
    </div>
  );
});
