import { useState, useRef, useEffect, useCallback, memo } from "react";
import { MessageCircle, Sparkles, X, Send, Loader2, GripVertical, Plus } from "lucide-react";
import { useCanvasAssistantStore } from "../../../stores/canvasAssistantStore";
import type { CanvasAssistantCtx } from "../../../stores/canvasAssistantStore";
import ActivityFeed from "./ActivityFeed";

// Props 接口：AI 助手组件的所有外部输入属性
interface AIAssistantProps {
  canvasId?: string | null;                                // 当前画布 ID（null = 未保存的新画布草稿），用于会话隔离
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
  onEnsureCanvas?: () => Promise<string>;                 // 画布草稿（canvasId=null）时先创建画布，确保会话绑定真实 canvas_id
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


export default memo(function AIAssistant({
  canvasId,
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
  onEnsureCanvas,
}: AIAssistantProps) {
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

  // 面板展开状态（提升到全局 store：切路由回来后仍保持展开）
  const open = useCanvasAssistantStore((s) => s.open);
  const setOpen = useCanvasAssistantStore((s) => s.setOpen);
  // 画布助手全局状态订阅：组件卸载后 store 仍存活，SSE 流与对话状态不随路由销毁
  const messages = useCanvasAssistantStore((s) => s.messages);
  const steps = useCanvasAssistantStore((s) => s.steps);
  const meta = useCanvasAssistantStore((s) => s.meta);
  const isStreaming = useCanvasAssistantStore((s) => s.isStreaming);
  const canvasSessions = useCanvasAssistantStore((s) => s.canvasSessions);
  const curSessionId = useCanvasAssistantStore((s) => s.curSessionId);

  const [inputValue, setInputValue] = useState("");       // 输入框当前文本
  const messagesEndRef = useRef<HTMLDivElement>(null);     // 消息列表底部引用，用于自动滚动
  const inputRef = useRef<HTMLInputElement>(null);          // 输入框引用
  const panelRef = useRef<HTMLDivElement>(null);            // 面板容器引用

  // 流式状态上报：父组件据此在 Agent 落块期间禁用画布拖动，避免位置冲突
  useEffect(() => {
    onStreamingChange?.(isStreaming);
  }, [isStreaming, onStreamingChange]);

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

  // 数据源变化时更新欢迎语（store 内只更新首条 welcome 消息）
  useEffect(() => {
    useCanvasAssistantStore.getState().syncWelcome({
      canvasId: canvasId ?? null,
      datasourceId,
      fieldMeta,
      canvasBlocks,
      currentDimensions,
      currentMeasures,
      currentChartType,
      allDatasources,
      onApplyChartConfig,
      onCanvasAction,
      ensureCanvas: onEnsureCanvas,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasourceId, fieldMeta, allDatasources]);

  // 构建一次通用的画布上下文（事件触发时携带当前最新闭包值，供 store 发起流式请求）
  const buildCtx = useCallback(
    (): CanvasAssistantCtx => ({
      canvasId: canvasId ?? null,
      datasourceId,
      fieldMeta,
      canvasBlocks,
      currentDimensions,
      currentMeasures,
      currentChartType,
      allDatasources,
      onApplyChartConfig,
      onCanvasAction,
      ensureCanvas: onEnsureCanvas,
    }),
    [canvasId, datasourceId, fieldMeta, canvasBlocks, currentDimensions, currentMeasures, currentChartType, allDatasources, onApplyChartConfig, onCanvasAction, onEnsureCanvas],
  );

  // 画布切换：重置当前会话与消息（防跨画布串记忆），并拉取该画布的会话列表。
  // 注意：画布"落盘"（canvasId 从 null → 真实 ID，AI 对话中 ensureCanvas 触发）不算切换，
  // 只刷新会话列表，不能打断正在进行的对话或清空当前会话。
  const prevCanvasIdRef = useRef<string | null>(null);
  useEffect(() => {
    const cid = canvasId ?? null;
    const prev = prevCanvasIdRef.current;
    prevCanvasIdRef.current = cid;
    // 首次挂载 / 值未变 / 画布落盘（null → ID）：只刷新会话列表
    if (prev === cid || (prev === null && cid)) {
      void useCanvasAssistantStore.getState().refreshSessions(cid);
      return;
    }
    // 真正的画布切换：ID → 另一个 ID / ID → null → 交 store 清空会话状态防串记忆
    useCanvasAssistantStore.getState().resetForCanvas(buildCtx());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canvasId]);

  // 会话列表就绪后：默认选中最近一条会话并加载其历史（刷新页面/切回画布时恢复）
  useEffect(() => {
    void useCanvasAssistantStore.getState().onCanvasReady(canvasId ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canvasId, canvasSessions]);

  // 新消息到达时自动滚动到消息列表底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // 发送消息：委托给全局 store（SSE 消费在 store 内进行，组件卸载不中断流）
  const handleSend = async (text?: string) => {
    if (useCanvasAssistantStore.getState().isStreaming) return;
    const content = (text || inputValue).trim();
    if (!content) return;
    // 先同步清掉输入框（DOM + state），避免用户连按回车重复发送同一条内容
    if (inputRef.current) inputRef.current.value = "";
    setInputValue("");
    void useCanvasAssistantStore.getState().send(content, buildCtx());
  };

  // 输入框键盘事件：Enter 键发送消息（Shift+Enter 不拦截，用于换行）
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (useCanvasAssistantStore.getState().isStreaming) return;
      handleSend();
    }
  };

  /** 新对话：断开当前会话，下一次发送时后端按 canvas_id 新建独立会话（画布内多会话） */
  const handleNewSession = () => {
    useCanvasAssistantStore.getState().newConversation(buildCtx());
  };

  /** 切换画布内历史会话：换 session_id 并加载该会话消息 */
  const handleSwitchSession = (sid: string) => {
    void useCanvasAssistantStore.getState().switchSession(sid, buildCtx());
  };

  // 注意：组件卸载时【不再主动断流】。SSE 与对话状态驻留于全局 store，
  // 用户切到其它路由后任务继续在后台跑，切回画布时无缝恢复。

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
            <div className="flex items-center gap-1">
              <button
                className="p-1 rounded hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed"
                onClick={handleNewSession}
                disabled={isStreaming || !canvasId}
                title={canvasId ? "新对话（本画布内另开一条对话）" : "保存画布后即可开新对话"}
              >
                <Plus className="w-4 h-4 text-muted-foreground" />
              </button>
              <button className="p-1 rounded hover:bg-muted" onClick={() => setOpen(false)}>
                <X className="w-4 h-4 text-muted-foreground" />
              </button>
            </div>
          </div>

          {/* 会话切换条：列出本画布的历史对话（仅已保存画布显示） */}
          {canvasId && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-border-light bg-card">
              <select
                value={curSessionId ?? ""}
                onChange={(e) => { if (e.target.value) handleSwitchSession(e.target.value); }}
                disabled={isStreaming}
                className="flex-1 min-w-0 text-[11.5px] px-2 py-1 rounded border border-border bg-background text-foreground outline-none"
                title="切换本画布的历史对话"
              >
                <option value="">{canvasSessions.length > 0 ? "当前为新对话" : "暂无历史对话"}</option>
                {canvasSessions.map((s) => (
                  <option key={s.id} value={s.id}>{s.title || "未命名对话"}</option>
                ))}
              </select>
            </div>
          )}

          {/* 消息列表区域 — 可滚动，每条消息按角色分别左右对齐 */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
            {messages.map((msg, idx) => {
              // 仅最后一条 assistant 消息（非欢迎语）才嵌入 Agent 工作台
              const isLastAssistant = msg.role === "assistant" && idx === messages.length - 1 && msg.id !== "welcome";
              return (
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
                  ) : isStreaming ? (
                    // 内容为空 + 仍在流式接收中 → 显示"思考中..."加载动画
                    <span className="inline-flex items-center gap-1 text-muted-foreground">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      思考中...
                    </span>
                  ) : (
                    // 流已结束但 LLM 全程只调用工具、没有输出纯文本 → 给用户一个明确的完成提示，避免永远转圈
                    <span className="whitespace-pre-wrap text-muted-foreground">
                      已完成分析并更新画布，请查看工作台执行记录与画布内容。
                    </span>
                  )}
                  {/* Agent 工作台嵌入到最新 AI 回复气泡内，仅当有实际步骤时展示 */}
                  {isLastAssistant && steps.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border-light/60">
                      <ActivityFeed steps={steps} meta={meta ?? undefined} />
                    </div>
                  )}
                </div>
              </div>
              );
            })}
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
        onClick={() => { if (!ballDragMoved.current) setOpen(!open); }}
        title="AI 画布助手"
      >
        <MessageCircle className="w-5 h-5" />
      </button>
    </div>
  );
});
