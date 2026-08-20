import { useState, useRef, useEffect, useCallback, memo } from "react";
import { MessageCircle, Sparkles, X, Send, Loader2, GripVertical } from "lucide-react";
import { tokenStore } from "../../../api/client";
import { listMessages } from "../../../api/ai";
import ActivityFeed from "./ActivityFeed";
import type { FeedStep } from "./ActivityFeed";

// Props 接口：AI 助手组件的所有外部输入属性
interface AIAssistantProps {
  datasourceId: string | null;                            // 当前选中的数据源 ID
  fieldMeta: Array<{ name: string; data_type: string; category?: string }> | null;  // 当前数据源的字段元信息（名称、类型、分类）
  canvasBlocks?: Array<Record<string, any>>;              // 画布上已有的块（供 AI 感知现状，避免重复建图/支持改删）
  currentDimensions?: string[];                           // 当前图表中使用的维度字段列表
  currentMeasures?: Array<{ field: string; agg: string }>;  // 当前图表中使用的度量字段及其聚合方式
  currentChartType?: string;                              // 当前图表的类型（柱状图、折线图等）
  allDatasources?: Array<{ id: string; name: string; fields?: Array<{name: string; data_type: string}> }>;  // 所有可选数据源列表
  onApplyChartConfig?: (config: { chartType?: string; dimensions?: string[]; measures?: Array<{ field: string; agg: string }> }) => void;  // 应用 AI 推荐图表配置的回调
  onCanvasAction?: (action: any) => void;                 // 接收 canvas_action，交由父组件实时落块
  onStreamingChange?: (streaming: boolean) => void;       // 流式状态变化上报（父组件据此禁用画布拖动）
}

// 聊天消息的数据结构
interface ChatMessage {
  id: string;           // 消息唯一标识
  role: "user" | "assistant";  // 发送者角色：用户或 AI 助手
  content: string;      // 消息文本内容
}

/** 从显示内容中剥离 ```json、```sql 等代码块，防止原始结构数据暴露给用户 */
function stripCodeBlocks(text: string): string {
  return text
    .replace(/```json[\s\S]*?```/g, "")
    .replace(/```sql[\s\S]*?```/g, "")
    .replace(/```[\s\S]*?```/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** 将 AI 返回的 Markdown 文本渲染为 JSX 节点，支持标题、引用、列表、分割线等格式 */
function renderMarkdown(text: string): React.ReactNode[] {
  const lines = text.split("\n");
  const nodes: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 空行 → 渲染为段落间距
    if (!line.trim()) {
      nodes.push(<div key={i} className="h-2" />);
      i++;
      continue;
    }

    // ### 三级标题（彩色、较小字号）
    if (/^###\s/.test(line)) {
      nodes.push(
        <div key={i} className="text-[13px] font-semibold text-ai mt-2 mb-1">
          {line.replace(/^###\s+/, "")}
        </div>
      );
      i++;
      continue;
    }

    // ## 二级标题（彩色、显眼，带下划线）
    if (/^##\s/.test(line)) {
      nodes.push(
        <div key={i} className="text-[14px] font-bold text-ai mt-3 mb-1.5 pb-1 border-b border-ai/20">
          {line.replace(/^##\s+/, "")}
        </div>
      );
      i++;
      continue;
    }

    // --- 水平分割线
    if (/^---+$/.test(line.trim())) {
      nodes.push(<div key={i} className="my-2 border-t border-border-light" />);
      i++;
      continue;
    }

    // > 引用/提示块（左侧带色条的高亮框）
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

    // - 无序列表项
    if (/^-\s/.test(line)) {
      nodes.push(
        <div key={i} className="flex gap-1.5 pl-1 text-[12px] leading-relaxed">
          <span className="text-ai flex-shrink-0 mt-px">&bull;</span>
          <span>{renderInline(line.replace(/^-\s+/, ""))}</span>
        </div>
      );
      i++;
      continue;
    }

    // 有序列表 1. 2. 等
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

    // **粗体标题** 独立一行 → 标签样式高亮显示
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

    // 普通段落文本
    nodes.push(
      <div key={i} className="text-[12px] leading-relaxed">
        {renderInline(line)}
      </div>
    );
    i++;
  }

  return nodes;
}

/** 渲染单行文本中的行内格式：**粗体** 和 `行内代码` */
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


/** 工具名 → 中文名（驱动工作台默认步骤标题） */
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


export default memo(function AIAssistant({
  datasourceId,
  fieldMeta,
  canvasBlocks,
  currentDimensions,
  currentMeasures,
  currentChartType,
  allDatasources,
  onApplyChartConfig,
  onCanvasAction,
  onStreamingChange,
}: AIAssistantProps) {
  // 面板展开/折叠状态
  const [open, setOpen] = useState(false);

  // ---------- 悬浮球拖拽状态 ----------
  const [ballPos, setBallPos] = useState<{ x: number; y: number } | null>(null);  // 悬浮球当前位置（null 表示使用默认右下角位置）
  const [isDraggingBall, setIsDraggingBall] = useState(false);  // 是否正在拖拽悬浮球
  const ballDragStart = useRef({ x: 0, y: 0 });  // 拖拽开始时鼠标的屏幕坐标
  const ballPosStart = useRef({ x: 0, y: 0 });    // 拖拽开始时悬浮球的位置
  const ballDragMoved = useRef(false);             // 标记拖拽过程中是否真正移动过（区分点击与拖拽）

  // ---------- 面板拖拽移动状态 ----------
  const [pos, setPos] = useState({ x: 0, y: 0 });  // 面板相对于初始位置的偏移量
  const [isDragging, setIsDragging] = useState(false);  // 是否正在拖拽面板标题栏
  const dragStart = useRef({ x: 0, y: 0 });        // 拖拽开始时鼠标位置
  const posStart = useRef({ x: 0, y: 0 });          // 拖拽开始时面板偏移量

  // ---------- 面板缩放状态 ----------
  const [size, setSize] = useState({ w: 380, h: 480 });  // 面板当前宽高（默认 380x480）
  const [isResizing, setIsResizing] = useState(false);     // 是否正在缩放面板
  const resizeStart = useRef({ x: 0, y: 0, w: 0, h: 0 }); // 缩放开始时鼠标位置和面板尺寸

  // 聊天消息列表，初始包含一条根据数据源状态生成的欢迎语
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: datasourceId && fieldMeta?.length
        ? `你好！我已了解当前数据源，共有 ${fieldMeta.length} 个字段。你可以让我帮你分析数据、推荐图表。`
        : "你好！我是 AI 画布助手。先选择数据源，我就能帮你分析数据和配置图表。",
    },
  ]);
  const [inputValue, setInputValue] = useState("");       // 输入框当前文本
  const [isStreaming, setIsStreaming] = useState(false);   // 是否正在接收 AI 流式响应
  {/* 流式状态上报：父组件据此在 Agent 落块期间禁用画布拖动，避免位置冲突 */}
  useEffect(() => {
    onStreamingChange?.(isStreaming);
  }, [isStreaming, onStreamingChange]);
  // Agent 工作台步骤时间线状态
  const [steps, setSteps] = useState<FeedStep[]>([]);     // 板内步骤（tool_call/tool_result 驱动）
  const runSeq = useRef(0);                               // 步骤自增 id 计数器
  const messagesEndRef = useRef<HTMLDivElement>(null);     // 消息列表底部引用，用于自动滚动
  const inputRef = useRef<HTMLInputElement>(null);          // 输入框引用
  const panelRef = useRef<HTMLDivElement>(null);            // 面板容器引用
  const sessionIdRef = useRef<string | null>(localStorage.getItem("canvas_session_id"));
  const sessionLoaded = useRef(false);

  // 悬浮球鼠标按下事件：进入拖拽状态，记录初始位置
  const onBallMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingBall(true);
    ballDragMoved.current = false;
    ballDragStart.current = { x: e.clientX, y: e.clientY };
    const currentPos = ballPos ?? { x: window.innerWidth - 72, y: window.innerHeight - 72 };
    ballPosStart.current = currentPos;
  }, [ballPos]);

  // 面板标题栏鼠标按下事件：进入拖拽移动状态，记录初始偏移量
  const onTitleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    dragStart.current = { x: e.clientX, y: e.clientY };
    posStart.current = { x: pos.x, y: pos.y };
  }, [pos]);

  // 面板右下角缩放手柄鼠标按下事件：进入缩放状态，记录初始尺寸
  const onResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsResizing(true);
    resizeStart.current = { x: e.clientX, y: e.clientY, w: size.w, h: size.h };
  }, [size]);

  // 全局鼠标移动/松开事件监听：处理拖拽移动、拖拽缩放逻辑
  useEffect(() => {
    if (!isDraggingBall && !isDragging && !isResizing) return;

    const onMove = (e: MouseEvent) => {
      // 拖拽悬浮球：更新 ballPos，限制在视口范围内
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
      // 拖拽面板：更新偏移量 pos
      if (isDragging) {
        setPos({
          x: posStart.current.x + (e.clientX - dragStart.current.x),
          y: posStart.current.y + (e.clientY - dragStart.current.y),
        });
      }
      // 缩放面板：更新尺寸 size（宽 320~700，高 320~800）
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

  // 数据源变化时更新欢迎语，展示字段数量或可选数据源列表
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

  // 加载已有会话的历史消息（页面刷新后恢复）
  useEffect(() => {
    if (sessionLoaded.current) return;
    sessionLoaded.current = true;
    const sid = sessionIdRef.current;
    if (!sid) return;
    (async () => {
      try {
        const msgs = await listMessages(sid);
        if (msgs.length > 0) {
          setMessages(msgs.map(m => ({
            id: m.id,
            role: m.role as "user" | "assistant",
            content: m.content,
          })));
        }
      } catch {
        sessionIdRef.current = null;
        localStorage.removeItem("canvas_session_id");
      }
    })();
  }, []);

  // 新消息到达时自动滚动到消息列表底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // 发送消息的核心处理函数：校验输入、构建请求、处理 SSE 流式响应
  const handleSend = async (text?: string) => {
    const content = (text || inputValue).trim();
    if (!content || isStreaming) return;
    // 未选择数据源时给出提示，不发起请求
    if (!datasourceId) {
      setMessages(prev => [...prev, { id: `e-${Date.now()}`, role: "assistant", content: "请先在左侧选择一个数据源" }]);
      return;
    }
    setInputValue("");

    // 添加用户消息
    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: "user", content };
    setMessages(prev => [...prev, userMsg]);

    // 预先创建一条空的 AI 消息占位，后续流式追加内容
    const assistantId = `a-${Date.now()}`;
    const assistantMsg: ChatMessage = { id: assistantId, role: "assistant", content: "" };
    setMessages(prev => [...prev, assistantMsg]);

    let assistantContent = "";
    setIsStreaming(true);

    const token = tokenStore.getAccess();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1';

    // 校验当前图表的维度和度量字段是否在数据源字段列表中，过滤掉已删除的字段
    const validFieldNames = new Set((fieldMeta ?? []).map(f => f.name).concat((fieldMeta ?? []).map(f => f.name.toLowerCase())));
    const cleanDims = (currentDimensions ?? []).filter(
      d => validFieldNames.has(d) || validFieldNames.has(d.toLowerCase())
    );
    const cleanMeasures = (currentMeasures ?? []).filter(
      m => validFieldNames.has(m.field) || validFieldNames.has(m.field.toLowerCase())
    );
    const hasValidCurrentConfig = cleanDims.length > 0 || cleanMeasures.length > 0;

    // 构建 canvas_context：包含当前图表配置（如有）和完整字段列表，供 AI 参考
    const canvasContext: Record<string, unknown> = {};
    if (hasValidCurrentConfig) {
      canvasContext.currentConfig = { dimensions: cleanDims, measures: cleanMeasures, chartType: currentChartType };
    }
    // 始终传递完整字段列表，即使没有选中图表，AI 也能据此智能推荐
    canvasContext.availableFields = (fieldMeta ?? []).map(f => ({ name: f.name, data_type: f.data_type, category: f.category }));
    // 传递画布已有块摘要，让 AI 感知现状（避免重复建图、支持改/删已有块）
    if (Array.isArray(canvasBlocks) && canvasBlocks.length) {
      canvasContext.blocks = canvasBlocks
        .filter((b) => b && b.type === "chart")
        .map((b) => ({
          block_id: b.blockId,
          title: b.title,
          chartType: b.chartType,
          dimensions: b.queryConfig?.dimensions ?? b.dimensions ?? [],
          measures: b.queryConfig?.measures ?? b.measures ?? [],
        }));
    }
    // 每次新任务重置工作台步骤时间线
    setSteps([]);

    try {
      // 发起 POST 请求，后端返回 SSE（Server-Sent Events）流
      const response = await fetch(`${baseUrl}/ai/canvas/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({
          datasource_id: datasourceId,
          session_id: sessionIdRef.current,
          message: content,
          canvas_context: canvasContext,
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err?.detail?.message || err?.error?.message || `HTTP ${response.status}`);
      }

      // 读取 SSE 流，逐行解析 data: 前缀的 JSON 事件
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
              // 'message': AI 返回的文本增量，逐段追加到助手消息中
              case 'message':
                assistantContent += event.delta;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              // 'tool_call': Agent 开始调用某工具 → 在当前运行步骤挂一个工具 chip（running）
              case 'tool_call': {
                const stepId = `${runSeq.current}`;
                // 若当前还没有运行中的步骤，创建默认步骤
                setSteps(prev => {
                  const runIdx = [...prev].reverse().findIndex(s => s.status === "run");
                  const hasRun = runIdx !== -1;
                  if (!hasRun) runSeq.current += 1;
                  const idx = hasRun ? prev.length - 1 - runIdx : prev.length;
                  const next = prev.slice();
                  if (!hasRun) {
                    next.push({ id: `${runSeq.current}`, title: `执行 ${TOOL_FALLBACK_NAME(event?.name ?? "工具")}`, status: "run", tools: [] });
                    return next;
                  }
                  next[idx] = { ...next[idx], tools: [...next[idx].tools, { name: event.name, args: event.args, status: "run" }] };
                  return next;
                });
                void stepId;
                break;
              }
              // 'tool_result': 工具执行完成 → 更新对应 chip 状态
              case 'tool_result': {
                const isErr = (() => {
                  try {
                    const r = event.result ? JSON.parse(event.result) : null;
                    return !!(r && r.error);
                  } catch { return false; }
                })();
                setSteps(prev => prev.map((s, i) =>
                  i === prev.length - 1
                    ? {
                        ...s,
                        tools: s.tools.map((t, j) =>
                          j === s.tools.length - 1 ? { ...t, result: event.result, status: isErr ? "err" : "ok" } : t
                        ),
                      }
                    : s
                ));
                break;
              }
              // 'canvas_action': Agent 的落块指令 → 转交父组件实时渲染，并给出提示
              case 'canvas_action': {
                assistantContent += `\n\n> 已${ACTION_LABEL[event.action] ?? event.action}: ${actionDesc(event)}\n`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                if (onCanvasAction) onCanvasAction(event);
                break;
              }
              // 'step'：后端显式步骤事件（如"正在启动多工具编排"）
              case 'step':
                runSeq.current += 1;
                setSteps(prev => [...prev, { id: `${runSeq.current}`, title: event.title ?? "执行中", status: "run", tools: [] }]);
                break;
              // 'chart'：兼容旧行为，保留但不再强制应用，仅作提示
              case 'chart':
                break;
              // 兼容旧事件
              // 'query_result': 查询结果数据，由 AI 自行处理，不显示原始数据表
              case 'query_result': {
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              }
              // 'query_error': AI 查询出错，将错误信息附加到消息中
              case 'query_error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              // 'chart_config': AI 推荐的图表配置到达，显示提示并调用 onApplyChartConfig 回调应用配置（旧链路兼容）
              case 'chart_config':
                assistantContent += `\n\n[图表配置已应用]`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                if (onApplyChartConfig && event.config) {
                  onApplyChartConfig(event.config);
                }
                break;
              // 'chart_config_error': 图表配置生成失败
              case 'chart_config_error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              // 'error': 通用错误事件
              case 'error':
                assistantContent += `\n\n> ${event.message}`;
                setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantContent } : m));
                break;
              // 'session_created': 后端新建了会话，保存 sessionId
              case 'session_created':
                sessionIdRef.current = event.session_id;
                localStorage.setItem("canvas_session_id", event.session_id);
                break;
              // 'done': 任务结束，标记当前运行步骤完成
              case 'done':
                setSteps(prev => prev.map(s => s.status === "run" ? { ...s, status: "done", tools: s.tools } : s));
                break;
            }
          } catch { /* 跳过解析失败的非法事件行 */ }
        }
      }
    } catch (err: any) {
      setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: `[连接失败] ${err.message}` } : m));
    } finally {
      setIsStreaming(false);
    }
  };

  // 输入框键盘事件：Enter 键发送消息（Shift+Enter 不拦截，用于换行）
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // 面板样式：结合拖拽偏移和缩放尺寸，拖拽/缩放时禁用文本选择
  const panelStyle: React.CSSProperties = {
    width: size.w,
    height: size.h,
    transform: `translate(${pos.x}px, ${pos.y}px)`,
    cursor: isDragging ? "grabbing" : undefined,
    userSelect: isDragging || isResizing ? "none" : undefined,
  };

  return (
    // 外层容器：使用 ballPos 定位悬浮球（默认在右下角），设置 z-50 确保浮层在最上层
    <div className="z-50" style={ballPos ? { position: 'fixed', left: ballPos.x, top: ballPos.y } : { position: 'fixed', bottom: 24, right: 24 }}>
      {open && (
        <div
          ref={panelRef}
          className="absolute bottom-14 right-0 bg-white rounded-[14px] overflow-hidden flex flex-col shadow-float border border-border-light"
          style={panelStyle}
        >
          {/* 标题栏 — 可拖拽手柄 */}
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

          {/* 消息列表区域 — 可滚动，每条消息按角色分别左右对齐 */}
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
                    // AI 消息（非欢迎语）使用 Markdown 渲染；用户消息和欢迎语直接显示纯文本
                    msg.role === "assistant" && msg.id !== "welcome"
                      ? <div>{renderMarkdown(stripCodeBlocks(msg.content))}</div>
                      : <span className="whitespace-pre-wrap">{msg.content}</span>
                  ) : (
                    // 内容为空时显示"思考中..."加载动画
                    <span className="inline-flex items-center gap-1 text-muted-foreground">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      思考中...
                    </span>
                  )}
                </div>
              </div>
            ))}
            {steps.length > 0 && <ActivityFeed steps={steps} />}
            <div ref={messagesEndRef} />
          </div>

          {/* 快捷操作按钮 — 点击直接发送预设问题 */}
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

          {/* 输入区 — 文本输入框 + 发送按钮 */}
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

          {/* 右下角缩放手柄 — 拖拽可调整面板尺寸 */}
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

      {/* 悬浮按钮 — 点击展开/折叠面板；可拖拽移动位置 */}
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
