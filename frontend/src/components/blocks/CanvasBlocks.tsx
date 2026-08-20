import { useEffect, useMemo, useRef, useState } from "react";
import {
  MoreHorizontal,
  Trash2,
  Lightbulb,
  Wand2,
  X,
  Loader2,
  TrendingUp,
  AlertTriangle,
  Sparkles,
  Image as ImageIcon,
  Upload,
  Link2,
} from "lucide-react";
import type { ReactNode } from "react";
import { useInView } from "../../hooks/useInView";
import type {
  CanvasBlock,
  ChartQueryConfig,
  QueryResult,
} from "../../api/types";
import ChartRenderer from "../charts/ChartRenderer";
import { generateInsights, polishText } from "../../api/ai";
import { useToast } from "../ui/Toast";
import { detectAlignment } from "../../hooks/useBlockAlignment";
import type { AlignmentGuide, BlockBounds } from "../../hooks/useBlockAlignment";
import { estimateBlockHeight } from "../../utils/reportLayout";
import { useCrossFilterStore } from "../../stores/crossFilterStore";


{/* 可编辑文本组件：点击文本进入编辑模式（input/textarea），支持 Enter/Escape 提交或取消 */}
function EditableText({
  value,
  onCommit,
  multiline = true,
  className,
}: {
  value: string;
  onCommit: (next: string) => void;
  multiline?: boolean;
  className?: string;
}) {
  {/* editing: 是否处于编辑状态；draft: 编辑中的文本草稿 */}
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  {/* 进入编辑模式：将当前值填入草稿 */}
  const enter = () => {
    setDraft(value);
    setEditing(true);
  };

  {/* 提交编辑：仅当草稿与原值不同时才触发 onCommit 回调 */}
  const commit = () => {
    setEditing(false);
    if (draft !== value) onCommit(draft);
  };

  {/* 取消编辑：恢复草稿为原始值 */}
  const cancel = () => {
    setEditing(false);
    setDraft(value);
  };

  if (editing) {
    if (multiline) {
      return (
        <textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              cancel();
            } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              commit();
            }
          }}
          rows={Math.max(2, draft.split("\n").length)}
          className={`w-full px-2 py-1.5 text-[13px] leading-relaxed rounded-md border border-border focus:border-primary bg-white text-card-foreground focus:outline-none resize-y ${className ?? ""}`}
        />
      );
    }
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancel();
          }
        }}
        className={`w-full px-2 py-1 text-[15px] font-semibold rounded-md border border-border focus:border-primary bg-white text-foreground focus:outline-none ${className ?? ""}`}
      />
    );
  }

  return (
    <div
      onClick={enter}
      title="点击编辑"
      className={`cursor-text rounded-md hover:bg-muted/60 transition-colors px-1 py-0.5 -mx-1 -my-0.5 whitespace-pre-wrap break-words ${className ?? ""}`}
    >
      {value}
    </div>
  );
}

interface BlockWrapperProps {
  label: string;
  labelBg: string;
  labelText: string;
  onDelete?: () => void;
  extraToolbar?: ReactNode;
  selected?: boolean;
  onSelect?: () => void;
  isDragOver?: boolean;
  onDragOver?: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave?: () => void;
  onDrop?: (e: React.DragEvent<HTMLDivElement>) => void;
  children: ReactNode;
  blockWidth?: number;
  onWidthChange?: (w: number) => void;
  blockHeight?: number;
  onHeightChange?: (h: number) => void;
  defaultBlockWidth?: number;
  blockX?: number;
  blockY?: number;
  blockType?: string;
  onPositionChange?: (x: number, y: number) => void;
  /** 其他块的边界（画布坐标系），拖动时用于对齐吸附 */
  siblings?: BlockBounds[];
  /** 拖动中实时上报对齐辅助线，松手清空 */
  onGuidesChange?: (guides: AlignmentGuide[]) => void;
  /** 禁用位置拖动（如 Agent 流式输出期间，防止落块冲突） */
  dragDisabled?: boolean;
  /** 未选中时隐藏标签 */
  hideLabelWhenNotSelected?: boolean;
  /** 极简模式：无背景/阴影/内边距 */
  minimal?: boolean;
}

{/* BlockWrapper：画布中每个 block 的容器，支持绝对定位、宽度/高度拖拽、自由移动、选中态样式
   - 宽度拖拽：使用 document 级 mousemove/mouseup 事件，拖拽过程中实时更新 DOM 宽，松手后 commit
   - 高度拖拽：使用 setPointerCapture 在拖拽手柄上捕获指针事件，松手后 commit
   - 位置拖拽：按住标签栏拖动整个 block，使用 document 级事件
   - 选中态：显示蓝色描边 ring 和右上角操作栏（删除、更多）
   - hideLabelWhenNotSelected：未选中时隐藏类型标签，仅在选中或 hover 时显示 */}
function BlockWrapper({
  label,
  labelBg,
  labelText,
  onDelete,
  extraToolbar,
  selected = false,
  onSelect,
  isDragOver = false,
  onDragOver,
  onDragLeave,
  onDrop,
  children,
  blockWidth,
  onWidthChange,
  blockHeight,
  onHeightChange,
  defaultBlockWidth,
  blockX,
  blockY,
  onPositionChange,
  siblings,
  onGuidesChange,
  dragDisabled = false,
  hideLabelWhenNotSelected = false,
  minimal = false,
}: BlockWrapperProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  {/* 三个 ref 分别记录三种拖拽操作的起始状态，仅在拖拽进行中非空 */}
  const widthDragState = useRef<{ startX: number; startW: number } | null>(null);
  const heightDragState = useRef<{ startY: number; startH: number } | null>(null);
  const moveDragState = useRef<{ startX: number; startY: number; startLeft: number; startTop: number } | null>(null);

  {/* 宽度拖拽鼠标按下：记录起始位置和宽度，监听 document 级 mousemove/mouseup */}
  const handleWMouseDown = (e: React.MouseEvent) => {
    if (!onWidthChange) return;
    e.preventDefault();
    e.stopPropagation();
    const currentW = containerRef.current?.offsetWidth ?? 400;
    if (containerRef.current) {
      containerRef.current.style.width = `${currentW}px`;
    }
    widthDragState.current = { startX: e.clientX, startW: currentW };
    {/* 拖拽移动中：实时更新 DOM 宽度，限制范围 200~1400px */}
    const onMove = (ev: MouseEvent) => {
      if (!widthDragState.current) return;
      ev.preventDefault();
      const delta = ev.clientX - widthDragState.current.startX;
      const next = Math.max(200, Math.min(1400, widthDragState.current.startW + delta));
      const rounded = next;
      if (containerRef.current) {
        containerRef.current.style.width = `${rounded}px`;
      }
    };
    {/* 松手：读取最终宽度，通过 onWidthChange 回调 commit，清理事件监听 */}
    const onUp = () => {
      if (!widthDragState.current) return;
      const el = containerRef.current;
      const finalW = el ? parseInt(el.style.width || "0", 10) : widthDragState.current.startW;
      widthDragState.current = null;
      onWidthChange(finalW);
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp, { once: true });
  };

  {/* 高度拖拽按下：使用 setPointerCapture 在手柄元素上捕获指针（兼容触摸），记录起始高度 */}
  const handleHPointerDown = (e: React.PointerEvent) => {
    if (!onHeightChange) return;
    e.preventDefault();
    e.stopPropagation();
    const handle = e.currentTarget as HTMLDivElement;
    handle.setPointerCapture(e.pointerId);
    const el = containerRef.current;
    const currentH = el ? el.offsetHeight : 120;
    heightDragState.current = { startY: e.clientY, startH: currentH };
  };
  {/* 高度拖拽移动中：实时更新 DOM 高度，限制范围 60~1200px */}
  const handleHPointerMove = (e: React.PointerEvent) => {
    if (!heightDragState.current || !onHeightChange) return;
    e.preventDefault();
    e.stopPropagation();
    const delta = e.clientY - heightDragState.current.startY;
    const next = Math.max(60, Math.min(1200, heightDragState.current.startH + delta));
    const rounded = next;
    if (containerRef.current) {
      containerRef.current.style.height = `${rounded}px`;
    }
  };
  {/* 高度拖拽松手：释放指针捕获，读取最终高度 commit */}
  const handleHPointerUp = (e: React.PointerEvent) => {
    if (!heightDragState.current || !onHeightChange) return;
    (e.currentTarget as HTMLDivElement).releasePointerCapture(e.pointerId);
    const el = containerRef.current;
    const finalH = el ? parseInt(el.style.height || "0") : heightDragState.current.startH;
    onHeightChange(finalH);
    heightDragState.current = null;
  };

  {/* 自由拖动标题栏鼠标按下：计算 block 相对于父容器的偏移，通过 document 级事件实现自由移动
     - 拖动中调用 detectAlignment 做网格吸附 + 边缘对齐，并实时上报辅助线
     - 按住 Alt 临时禁用吸附；dragDisabled（流式输出期间）完全禁止拖动 */}
  const handleMoveMouseDown = (e: React.MouseEvent) => {
    if (!onPositionChange || dragDisabled) return;
    // 只响应鼠标左键
    if (e.button !== 0) return;
    // 阻止文本选中 / 让点击选中
    e.preventDefault();
    e.stopPropagation();
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const parent = el.offsetParent as HTMLElement | null;
    const parentRect = parent ? parent.getBoundingClientRect() : { left: 0, top: 0 };
    // 计算当前 block 相对于父容器的左上角位置
    const startLeft = rect.left - parentRect.left;
    const startTop = rect.top - parentRect.top;
    moveDragState.current = {
      startX: e.clientX,
      startY: e.clientY,
      startLeft,
      startTop,
    };
    {/* 拖拽移动中：根据鼠标位移更新 block 的 left/top，限制不小于 0 */}
    const onMove = (ev: MouseEvent) => {
      if (!moveDragState.current) return;
      ev.preventDefault();
      const dx = ev.clientX - moveDragState.current.startX;
      const dy = ev.clientY - moveDragState.current.startY;
      let nextLeft = Math.max(0, moveDragState.current.startLeft + dx);
      let nextTop = Math.max(0, moveDragState.current.startTop + dy);
      {/* 对齐吸附：网格 8px + 其他块边缘 4px 阈值，Alt 按下时跳过 */}
      const current: BlockBounds = {
        id: "dragging",
        x: nextLeft,
        y: nextTop,
        width: el.offsetWidth,
        height: el.offsetHeight,
      };
      const aligned = detectAlignment(current, siblings ?? [], ev.altKey);
      nextLeft = aligned.position.x;
      nextTop = aligned.position.y;
      el.style.left = `${nextLeft}px`;
      el.style.top = `${nextTop}px`;
      onGuidesChange?.(aligned.guides);
    };
    {/* 松手：读取最终位置，通过 onPositionChange 回调 commit，清空辅助线 */}
    const onUp = () => {
      if (!moveDragState.current) return;
      const cur = containerRef.current;
      const finalLeft = cur ? parseFloat(cur.style.left || "0") : moveDragState.current.startLeft;
      const finalTop = cur ? parseFloat(cur.style.top || "0") : moveDragState.current.startTop;
      onPositionChange(Math.round(finalLeft), Math.round(finalTop));
      moveDragState.current = null;
      onGuidesChange?.([]);
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  return (
    <div
      ref={containerRef}
      onClick={onSelect}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      style={{
        position: "absolute",
        left: typeof blockX === "number" ? `${blockX}px` : "0px",
        top: typeof blockY === "number" ? `${blockY}px` : "0px",
        ...(blockWidth
          ? { width: `${blockWidth}px` }
          : defaultBlockWidth
            ? { width: `${defaultBlockWidth}px` }
            : { width: "100%" }),
        ...(blockHeight ? { height: `${blockHeight}px` } : {}),
      }}
      className={`${
        minimal
          ? "bg-transparent p-0"
          : "bg-white rounded-[10px] p-5 shadow-card"
      } group transition-shadow select-none flex flex-col ${
        blockHeight ? "overflow-auto" : ""
      } ${
        selected
          ? "ring-2 ring-primary shadow-md z-10"
          : minimal
            ? ""
            : "hover:shadow-md"
      } ${isDragOver ? "ring-2 ring-primary/60 bg-primary-light/30" : ""}`}
    >
      <div className="absolute top-3 right-3 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        {extraToolbar}
        {onDelete ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="p-1 rounded-[6px] hover:bg-danger-light text-muted-foreground hover:text-danger"
            title="删除"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        ) : null}
        <button
          onClick={(e) => e.stopPropagation()}
          className="p-1 rounded-[6px] hover:bg-muted text-muted-foreground"
        >
          <MoreHorizontal className="w-4 h-4" />
        </button>
      </div>
      {(!hideLabelWhenNotSelected || selected) ? (
        <span
          onMouseDown={onPositionChange && !dragDisabled ? handleMoveMouseDown : undefined}
          className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium mb-3 ${labelBg} ${labelText} ${
            onPositionChange && !dragDisabled ? "cursor-move" : ""
          }`}
          title={onPositionChange && !dragDisabled ? "按住拖动以自由移动（按住 Alt 临时关闭吸附）" : undefined}
        >
          {label}
        </span>
      ) : null}
      {children}
      {/* 右侧宽度拖拽手柄 */}
      <div
        onMouseDown={handleWMouseDown}
        onDragStart={(e) => e.preventDefault()}
        style={{ touchAction: "none" }}
        className="absolute right-0 top-0 bottom-0 w-5 cursor-ew-resize flex items-center justify-center z-20"
        title="拖动调整宽度"
      >
        <span className="w-1.5 h-8 bg-primary/50 hover:bg-primary rounded-full transition-colors" />
      </div>
      {/* 底部高度拖拽手柄 */}
      <div
        onPointerDown={handleHPointerDown}
        onPointerMove={handleHPointerMove}
        onPointerUp={handleHPointerUp}
        onPointerCancel={handleHPointerUp}
        onDragStart={(e) => e.preventDefault()}
        style={{ touchAction: "none" }}
        className={`absolute -bottom-2 left-0 right-0 h-4 cursor-ns-resize flex items-center justify-center z-10 ${
          selected ? "opacity-100" : "opacity-0 group-hover:opacity-100"
        } transition-opacity`}
        title="拖动调整高度"
      >
        <span className="w-12 h-1 bg-primary/40 hover:bg-primary/60 rounded-full" />
      </div>
    </div>
  );
}

{/* CanvasBlocks 主组件的 Props 类型 */}
interface CanvasBlocksProps {
  blocks: CanvasBlock[];
  chartConfigs: Record<string, ChartQueryConfig>;
  chartResults: Record<string, QueryResult>;
  loadingCharts: Record<string, boolean>;
  onDeleteBlock?: (index: number) => void;
  datasourceId?: string | null;
  onInsertBlockAfter?: (index: number, block: CanvasBlock) => void;
  onUpdateBlockContent?: (index: number, content: string) => void;
  onUpdateBlock?: (index: number, patch: Record<string, unknown>) => void;
  selectedBlockIdx?: number | null;
  onSelectBlock?: (index: number | null) => void;
  highlightBlockId?: string | null;
  /** Agent 流式输出期间为 true：禁止拖动块，防止与落块位置冲突 */
  isStreaming?: boolean;
}

{/* 类型守卫：判断 block 是否为 h1/h2/text 文本类型，并收窄类型以访问 content 属性 */}
function isTextBlock(b: CanvasBlock): b is CanvasBlock & { type: "text" | "h1" | "h2"; content: string } {
  return b.type === "text" || b.type === "h1" || b.type === "h2";
}

{/* 类型守卫：判断 block 是否为图表类型，要求 type === "chart" 且 blockId 为字符串 */}
function isChartBlock(
  b: CanvasBlock
): b is CanvasBlock & { type: "chart"; blockId: string; title?: string; renderer?: string; palette?: string; height?: number } {
  return b.type === "chart" && typeof (b as { blockId?: unknown }).blockId === "string";
}

{/* 类型守卫：判断 block 是否为图片类型 */}
function isImageBlock(b: CanvasBlock): b is CanvasBlock & { type: "image"; src: string; alt?: string; width?: number; height?: number } {
  return b.type === "image";
}

{/* LazyChartBlock：图表懒加载容器，仅当组件进入视口时才渲染子元素，否则显示骨架屏占位 */}
function LazyChartBlock({ children }: { children: ReactNode }) {
  const { ref, inView } = useInView();

  return (
    <div ref={ref} style={{ minHeight: 400 }}>
      {inView ? (
        children
      ) : (
        <div className="animate-pulse bg-muted rounded-[10px]" style={{ height: 400 }} />
      )}
    </div>
  );
}

{/* ImageBlockView：图片块视图，支持三种方式设置图片—上传、URL 输入、Ctrl+V 粘贴
   - selected：选中状态下显示额外控制栏（宽/高输入、alt 编辑、替换按钮）
   - mode：切换上传模式（upload）与 URL 模式（url）
   - dragOver：拖拽文件悬停时的视觉反馈 */}
function ImageBlockView({
  block,
  onUpdate,
  selected,
}: {
  block: CanvasBlock & { type: "image"; src: string; alt?: string; width?: number; height?: number };
  onUpdate: (patch: Record<string, unknown>) => void;
  selected: boolean;
}) {
  const toast = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  {/* mode: "upload" 显示文件上传区，"url" 显示 URL 输入框；根据已有 src 自动推断初始模式 */}
  const [mode, setMode] = useState<"upload" | "url">(block.src?.startsWith("http") || block.src?.startsWith("data:") ? (block.src?.startsWith("http") ? "url" : "upload") : "upload");
  const [urlDraft, setUrlDraft] = useState(block.src ?? "");
  const [altDraft, setAltDraft] = useState(block.alt ?? "");
  const [dragOver, setDragOver] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  {/* 监听全局粘贴事件：当图片块处于选中态时，允许用户直接从剪贴板粘贴图片 */}
  useEffect(() => {
    const handler = (e: ClipboardEvent) => {
      if (!selected) return;
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const item of Array.from(items)) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            e.preventDefault();
            const reader = new FileReader();
            reader.onload = () => {
              const dataUrl = String(reader.result ?? "");
              if (dataUrl) {
                onUpdate({ src: dataUrl, alt: file.name });
                setMode("upload");
              }
            };
            reader.readAsDataURL(file);
          }
          return;
        }
      }
    };
    window.addEventListener("paste", handler);
    return () => window.removeEventListener("paste", handler);
  }, [selected, onUpdate]);

  {/* 读取并验证图片文件：只接受图片类型且不超过 5MB，转换为 data:URL 后更新 block */}
  const readFile = (file: File) => {
    if (!file.type.startsWith("image/")) {
      toast.warning("只支持图片文件");
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      toast.warning("图片大小不能超过 5MB");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result ?? "");
      if (dataUrl) {
        onUpdate({ src: dataUrl, alt: file.name });
      }
    };
    reader.readAsDataURL(file);
  };

  {/* 文件选择输入变化时触发读取 */}
  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) readFile(f);
    e.target.value = "";
  };

  {/* 拖拽文件松手时触发读取 */}
  const onDropFile = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) readFile(f);
  };

  {/* 应用 URL 方式加载图片 */}
  const applyUrl = () => {
    if (urlDraft.trim()) {
      onUpdate({ src: urlDraft.trim(), alt: altDraft || undefined });
    }
  };

  {/* 清除当前图片 */}
  const clearImage = () => {
    onUpdate({ src: "", alt: "" });
    setUrlDraft("");
  };

  const hasImage = !!block.src;

  return (
    <div ref={containerRef}>
      {!hasImage ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDropFile}
          className={`mt-2 border-2 border-dashed rounded-[10px] p-6 text-center transition-colors ${
            dragOver
              ? "border-primary bg-primary-light/40"
              : "border-border bg-muted/30"
          }`}
        >
          <ImageIcon className="w-10 h-10 mx-auto mb-2 text-muted-foreground" />
          <p className="text-[13px] text-card-foreground mb-1">
            拖拽图片到此处，或：
          </p>
          <div className="flex items-center justify-center gap-2 mt-2">
            <button
              onClick={(e) => {
                e.stopPropagation();
                fileInputRef.current?.click();
              }}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-[6px] text-[12px] font-medium bg-primary text-white hover:bg-primary-hover"
            >
              <Upload className="w-3.5 h-3.5" />
              点击上传
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                setMode(mode === "url" ? "upload" : "url");
              }}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-[6px] text-[12px] font-medium border border-border bg-white hover:bg-muted text-card-foreground"
            >
              <Link2 className="w-3.5 h-3.5" />
              {mode === "url" ? "切换上传" : "使用URL"}
            </button>
          </div>
          <p className="text-[11px] text-muted-foreground mt-3">
            提示：选中图片块后可直接 Ctrl+V 粘贴剪贴板里的图片
          </p>
          {mode === "url" ? (
            <div
              onClick={(e) => e.stopPropagation()}
              className="mt-3 flex gap-1.5 items-center"
            >
              <input
                type="url"
                value={urlDraft}
                onChange={(e) => setUrlDraft(e.target.value)}
                placeholder="粘贴图片URL，例如 https://..."
                className="flex-1 px-2.5 py-1.5 text-[12px] rounded-md border border-border bg-white focus:outline-none focus:ring-2 focus:ring-primary/40"
              />
              <button
                onClick={applyUrl}
                className="px-3 py-1.5 rounded-md text-[12px] font-medium bg-primary text-white hover:bg-primary-hover"
              >
                应用
              </button>
            </div>
          ) : null}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={onFileChange}
          />
        </div>
      ) : (
        <div className="mt-2 space-y-2">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDropFile}
            className={`relative border rounded-[10px] overflow-hidden bg-muted/30 ${
              dragOver ? "ring-2 ring-primary" : ""
            }`}
          >
            <img
              src={block.src}
              alt={block.alt || "画布图片"}
              className="block mx-auto"
              style={{
                maxWidth: "100%",
                width: block.width ? `${block.width}px` : "auto",
                maxHeight: block.height ? `${block.height}px` : "300px",
                height: block.height ? `${block.height}px` : "auto",
                objectFit: "contain",
              }}
              draggable={false}
            />
            <button
              onClick={(e) => {
                e.stopPropagation();
                clearImage();
              }}
              className="absolute top-2 right-2 p-1.5 rounded-full bg-black/60 text-white hover:bg-black/80"
              title="移除图片"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
          {selected ? (
            <div
              onClick={(e) => e.stopPropagation()}
              className="flex flex-wrap items-center gap-2 text-[11px]"
            >
              <button
                onClick={() => fileInputRef.current?.click()}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[6px] bg-muted hover:bg-muted/70 text-card-foreground"
              >
                <Upload className="w-3 h-3" />
                替换
              </button>
              <div className="flex items-center gap-1.5">
                <span className="text-muted-foreground">宽</span>
                <input
                  type="number"
                  min={100}
                  max={1200}
                  value={block.width ?? ""}
                  onChange={(e) => {
                    const v = e.target.value ? Number(e.target.value) : undefined;
                    onUpdate({ width: v, height: block.height });
                  }}
                  placeholder="自动"
                  className="w-16 px-1.5 py-0.5 rounded border border-border bg-white text-[11px] text-center"
                />
                <span className="text-muted-foreground">高</span>
                <input
                  type="number"
                  min={80}
                  max={800}
                  value={block.height ?? ""}
                  onChange={(e) => {
                    const v = e.target.value ? Number(e.target.value) : undefined;
                    onUpdate({ width: block.width, height: v });
                  }}
                  placeholder="300"
                  className="w-16 px-1.5 py-0.5 rounded border border-border bg-white text-[11px] text-center"
                />
              </div>
              <input
                type="text"
                value={altDraft}
                onChange={(e) => setAltDraft(e.target.value)}
                onBlur={() => onUpdate({ alt: altDraft || undefined })}
                placeholder="图片描述 (alt)"
                className="flex-1 min-w-[120px] px-2 py-1 rounded-md border border-border bg-white text-[11px] focus:outline-none focus:ring-1 focus:ring-primary/40"
              />
            </div>
          ) : null}
        </div>
      )}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={onFileChange}
      />
    </div>
  );
}

{/* CanvasBlocks：画布主渲染组件，负责渲染所有 block（h1/h2/text/image/chart）并管理 AI 洞察和润色弹窗
   - blocks: 画布上的所有 block 列表
   - chartConfigs / chartResults / loadingCharts: 各图表的查询配置、结果和加载状态
   - selectedBlockIdx: 当前选中的 block 索引
   - 内部维护 insights/polish 两组弹窗状态 */}
export default function CanvasBlocks({
  blocks,
  chartConfigs,
  chartResults,
  loadingCharts,
  onDeleteBlock,
  datasourceId,
  onInsertBlockAfter,
  onUpdateBlockContent,
  onUpdateBlock,
  selectedBlockIdx,
  onSelectBlock,
  highlightBlockId,
  isStreaming = false,
}: CanvasBlocksProps) {
  {/* AI 洞察弹窗状态：当前操作的 block 索引、加载中、错误信息和洞察列表 */}
  const [insightsModalBlockIdx, setInsightsModalBlockIdx] = useState<number | null>(null);
  {/* 对齐辅助线：拖动中由 BlockWrapper 实时上报，松手清空 */}
  const [guides, setGuides] = useState<AlignmentGuide[]>([]);
  {/* 跨图表联动筛选：点击维度值时切换全局 filter */}
  const toggleCrossFilter = useCrossFilterStore((s) => s.toggleFilter);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [insightsList, setInsightsList] = useState<
    Array<{ type: string; title: string; description: string; severity: string; related_fields?: string[] }>
  >([]);

  {/* AI 润色弹窗状态：当前操作的 block 索引、加载中、错误、结果和选中风格 */}
  const [polishModalIdx, setPolishModalIdx] = useState<number | null>(null);
  const [polishLoading, setPolishLoading] = useState(false);
  const [polishError, setPolishError] = useState<string | null>(null);
  const [polishResult, setPolishResult] = useState<{
    original: string;
    polished: string;
    style: string;
  } | null>(null);
  const [polishStyle, setPolishStyle] = useState("professional");

  {/* 打开 AI 洞察弹窗：根据图表配置请求 AI 生成数据洞察 */}
  const handleOpenInsights = async (blockIdx: number) => {
    if (!datasourceId) return;
    const block = blocks[blockIdx];
    if (!isChartBlock(block)) return;
    const config = chartConfigs[block.blockId];
    if (!config) return;

    setInsightsModalBlockIdx(blockIdx);
    setInsightsLoading(true);
    setInsightsError(null);
    setInsightsList([]);
    try {
      const result = await generateInsights({
        datasource_id: datasourceId,
        query_config: config as unknown as Record<string, unknown>,
      });
      setInsightsList(result.insights);
    } catch (e: any) {
      // 尝试从 Axios 错误响应中提取 detail 字段
      const axiosDetail = e?.response?.data?.detail;
      const msg = typeof axiosDetail === "string" ? axiosDetail : axiosDetail?.message;
      setInsightsError(msg || e?.message || "AI 洞察生成失败");
    } finally {
      setInsightsLoading(false);
    }
  };

  {/* 将洞察结果以文本块形式插入到图表块之后 */}
  const handleApplyInsight = (description: string) => {
    if (insightsModalBlockIdx != null && onInsertBlockAfter) {
      const textBlock: CanvasBlock = { type: "text", content: description };
      onInsertBlockAfter(insightsModalBlockIdx, textBlock);
    }
    closeInsights();
  };

  const closeInsights = () => {
    setInsightsModalBlockIdx(null);
    setInsightsList([]);
    setInsightsError(null);
  };

  // 打开润色弹窗（仅打开，不立即请求，等用户选完风格后再点"开始润色"）
  const handleOpenPolish = (blockIdx: number) => {
    const block = blocks[blockIdx];
    if (!isTextBlock(block)) return;
    setPolishModalIdx(blockIdx);
    setPolishLoading(false);
    setPolishError(null);
    setPolishResult(null);
  };

  // 用户选完风格后，点击"开始润色"才真正发起请求
  const handleStartPolish = async () => {
    if (polishModalIdx == null) return;
    const block = blocks[polishModalIdx];
    if (!isTextBlock(block)) return;
    setPolishLoading(true);
    setPolishError(null);
    setPolishResult(null);
    try {
      const text = typeof block.content === "string" ? block.content : "";
      const result = await polishText(text, polishStyle);
      setPolishResult(result);
    } catch (e) {
      setPolishError(e instanceof Error ? e.message : "AI 润色失败");
    } finally {
      setPolishLoading(false);
    }
  };

  const handleApplyPolish = () => {
    if (polishModalIdx != null && polishResult && onUpdateBlockContent) {
      onUpdateBlockContent(polishModalIdx, polishResult.polished);
    }
    closePolish();
  };

  const closePolish = () => {
    setPolishModalIdx(null);
    setPolishResult(null);
    setPolishError(null);
  };

  {/* 所有块的边界（画布坐标系），供拖动中的块做对齐吸附；无 width/height 的块用估算值 */}
  const allBounds: BlockBounds[] = useMemo(
    () =>
      (blocks ?? []).map((b, i) => {
        const d = b as Record<string, unknown>;
        const hasXY = typeof d.x === "number" && typeof d.y === "number";
        return {
          id: `block_${i}`,
          x: hasXY ? (d.x as number) : (i % 2) * 420,
          y: hasXY ? (d.y as number) : Math.floor(i / 2) * 400,
          width: typeof d.width === "number" ? (d.width as number) : 400,
          height: typeof d.height === "number" ? (d.height as number) : estimateBlockHeight(b),
        };
      }),
    [blocks],
  );

  if (!blocks || blocks.length === 0) {
    return (
      <div className="text-center py-16 text-muted-foreground text-[13px]">
        画布为空，从左侧拖拽字段或添加块开始创建报告
      </div>
    );
  }

  return (
    <>
    <div
      className="relative w-full"
      style={{ minHeight: "1600px" }}
    >
      {blocks.map((block, idx) => {
        const onDelete = onDeleteBlock ? () => onDeleteBlock(idx) : undefined;
        const isSelected = selectedBlockIdx === idx;
        const blockData = block as Record<string, unknown>;
        // 自由画布：每个 block 有自己的 x/y 坐标；老数据没有则按索引自动铺位（左侧垂直流）
        const hasXY = typeof blockData.x === "number" && typeof blockData.y === "number";
        const autoX = (idx % 2) * 420;
        const autoY = Math.floor(idx / 2) * 400;
        const blockX = hasXY ? (blockData.x as number) : autoX;
        const blockY = hasXY ? (blockData.y as number) : autoY;
        const onPosChange = onUpdateBlock
          ? (x: number, y: number) => onUpdateBlock(idx, { x, y })
          : undefined;
        {/* 对齐吸附：其他块的边界 + 辅助线上报；流式输出期间禁拖 */}
        const siblings = allBounds.filter((_, i) => i !== idx);
        const handleSelect = onSelectBlock
          ? () => onSelectBlock(isSelected ? null : idx)
          : undefined;
        const blockW = typeof blockData.width === "number" ? blockData.width as number : undefined;
        const onWChange = onUpdateBlock ? (w: number) => onUpdateBlock(idx, { width: w }) : undefined;
        const blockH = typeof blockData.height === "number" ? blockData.height as number : undefined;
        const onHChange = onUpdateBlock ? (h: number) => onUpdateBlock(idx, { height: h }) : undefined;

        if (isTextBlock(block)) {
          if (block.type === "h1") {
            return (
              <BlockWrapper
                key={idx}
                label="标题 · H1"
                labelBg="bg-success-light"
                labelText="text-success"
                onDelete={onDelete}
                selected={isSelected}
                onSelect={handleSelect}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
                blockX={blockX}
                blockY={blockY}
                blockType="h1"
                onPositionChange={onPosChange}
                siblings={siblings}
                onGuidesChange={setGuides}
                dragDisabled={isStreaming}
                hideLabelWhenNotSelected
              >
                <div>
                  <EditableText
                    value={block.content}
                    multiline={false}
                    className="text-[22px] font-bold text-foreground"
                    onCommit={(next) => onUpdateBlockContent?.(idx, next)}
                  />
                </div>
              </BlockWrapper>
            );
          }
          if (block.type === "h2") {
            return (
              <BlockWrapper
                key={idx}
                label="标题 · H2"
                labelBg="bg-primary-light"
                labelText="text-primary"
                onDelete={onDelete}
                selected={isSelected}
                onSelect={handleSelect}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
                blockX={blockX}
                blockY={blockY}
                blockType="h2"
                onPositionChange={onPosChange}
                siblings={siblings}
                onGuidesChange={setGuides}
                dragDisabled={isStreaming}
                hideLabelWhenNotSelected
              >
                <div>
                  <EditableText
                    value={block.content}
                    multiline={false}
                    className="text-[18px] font-semibold text-foreground"
                    onCommit={(next) => onUpdateBlockContent?.(idx, next)}
                  />
                </div>
              </BlockWrapper>
            );
          }
          return (
            <BlockWrapper
              key={idx}
              label="文本"
              labelBg="bg-muted"
              labelText="text-muted-foreground"
              onDelete={onDelete}
              selected={isSelected}
              onSelect={handleSelect}
              blockWidth={blockW}
              onWidthChange={onWChange}
              blockHeight={blockH}
              onHeightChange={onHChange}
                blockX={blockX}
                blockY={blockY}
                blockType="text"
                onPositionChange={onPosChange}
                siblings={siblings}
                onGuidesChange={setGuides}
                dragDisabled={isStreaming}
              hideLabelWhenNotSelected
              extraToolbar={
                <button
                  onClick={() => handleOpenPolish(idx)}
                  className="p-1 rounded-[6px] hover:bg-ai-light text-muted-foreground hover:text-ai"
                  title="AI 润色"
                >
                  <Wand2 className="w-4 h-4" />
                </button>
              }
            >
              <div>
                <EditableText
                  value={block.content}
                  className="text-[14px] leading-relaxed text-card-foreground"
                  onCommit={(next) => onUpdateBlockContent?.(idx, next)}
                />
              </div>
            </BlockWrapper>
          );
        }
        if (isImageBlock(block)) {
          return (
            <BlockWrapper
              key={idx}
              label="图片"
              labelBg="bg-muted"
              labelText="text-muted-foreground"
              onDelete={onDelete}
              selected={isSelected}
              onSelect={handleSelect}
              blockWidth={blockW}
              onWidthChange={onWChange}
              blockHeight={blockH}
              onHeightChange={onHChange}
                blockX={blockX}
                blockY={blockY}
                blockType="image"
                onPositionChange={onPosChange}
                siblings={siblings}
                onGuidesChange={setGuides}
                dragDisabled={isStreaming}
            >
              <div>
                <ImageBlockView
                  block={block}
                  selected={isSelected}
                  onUpdate={(patch) => onUpdateBlock?.(idx, patch)}
                />
              </div>
            </BlockWrapper>
          );
        }
        if (isChartBlock(block)) {
          const config = chartConfigs[block.blockId];
          const result = chartResults[block.blockId];
          const loading = loadingCharts[block.blockId] ?? false;
          const renderer = typeof block.renderer === "string" ? block.renderer as "recharts" | "echarts" : "recharts";
          const height = typeof block.height === "number" ? block.height : 240;
          return (
            <LazyChartBlock key={idx}>
              <BlockWrapper
                label="图表"
                labelBg="bg-muted"
                labelText="text-muted-foreground"
                onDelete={onDelete}
                selected={isSelected}
                onSelect={handleSelect}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
                blockX={blockX}
                blockY={blockY}
                blockType="chart"
                onPositionChange={onPosChange}
                siblings={siblings}
                onGuidesChange={setGuides}
                dragDisabled={isStreaming}
                defaultBlockWidth={400}
                extraToolbar={
                  <button
                    onClick={() => handleOpenInsights(idx)}
                    className="p-1 rounded-[6px] hover:bg-ai-light text-muted-foreground hover:text-ai"
                    title="AI 洞察"
                  >
                    <Lightbulb className="w-4 h-4" />
                  </button>
                }
              >
                <div className={`flex flex-col min-h-0 ${blockH ? "flex-1" : ""}`}>
                  <div className="flex items-center justify-between mb-2 flex-shrink-0">
                    <div className="text-[13px] font-semibold text-foreground">
                      <EditableText
                        value={block.title ?? "图表"}
                        multiline={false}
                        className="text-[13px] font-semibold text-foreground"
                        onCommit={(next) => onUpdateBlock?.(idx, { title: next || undefined })}
                      />
                    </div>
                  </div>
                  <div
                    id={block.blockId}
                    className={`flex-1 min-h-0 rounded-[6px] ${highlightBlockId === block.blockId ? "ring-2 ring-ai" : ""}`}
                    style={blockH ? undefined : { height }}
                  >
                    <ChartRenderer
                      config={config}
                      result={result}
                      loading={loading}
                      renderer={renderer}
                      palette={block.palette}
                      onDimensionClick={(dimension, value) => toggleCrossFilter({ field: dimension, value })}
                    />
                  </div>
                </div>
              </BlockWrapper>
            </LazyChartBlock>
          );
        }
        return null;
      })}

      {/* 对齐辅助线：拖动中由 BlockWrapper 上报，垂直/水平 1px 主色实线 */}
      {guides.map((g, i) =>
        g.type === "vertical" ? (
          <div
            key={`gv-${i}`}
            className="absolute bg-primary pointer-events-none z-50"
            style={{ left: g.position, top: g.start, width: 1, height: g.end - g.start }}
          />
        ) : (
          <div
            key={`gh-${i}`}
            className="absolute bg-primary pointer-events-none z-50"
            style={{ top: g.position, left: g.start, height: 1, width: g.end - g.start }}
          />
        ),
      )}
    </div>

    {insightsModalBlockIdx != null ? (
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <div className="absolute inset-0 bg-black/40" onClick={closeInsights} />
        <div className="relative bg-white rounded-lg shadow-xl w-[min(500px,92vw)] max-h-[80vh] flex flex-col">
          <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-foreground">AI 洞察</h3>
            <button onClick={closeInsights} className="p-1.5 rounded-md hover:bg-muted text-muted-foreground" title="关闭">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="px-5 py-4 overflow-auto flex-1 space-y-3">
            {insightsLoading ? (
              <div className="flex items-center justify-center py-8 text-[13px] text-muted-foreground">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                AI 正在分析数据...
              </div>
            ) : insightsError ? (
              <div className="px-3 py-2 rounded-md bg-danger-light text-danger text-[13px]">{insightsError}</div>
            ) : insightsList.length === 0 ? (
              <div className="text-center py-8 text-[13px] text-muted-foreground">暂无洞察结果</div>
            ) : (
              insightsList.map((insight, i) => {
                const Icon =
                  insight.type === "trend" ? TrendingUp :
                  insight.type === "anomaly" ? AlertTriangle :
                  Sparkles;
                const severityColor =
                  insight.severity === "high" ? "text-danger" :
                  insight.severity === "medium" ? "text-[#F59E0B]" :
                  "text-info";
                return (
                  <div key={i} className="p-3 rounded-[8px] border border-border-light">
                    <div className="flex items-center gap-2 mb-1">
                      <Icon className={`w-4 h-4 ${severityColor}`} />
                      <span className="text-[13px] font-semibold text-foreground">{insight.title}</span>
                      <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${severityColor} bg-opacity-10`}>
                        {insight.type === "trend" ? "趋势" : insight.type === "anomaly" ? "异常" : "机会"}
                      </span>
                    </div>
                    <p className="text-[12px] text-card-foreground leading-relaxed">{insight.description}</p>
                    {insight.related_fields && insight.related_fields.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {insight.related_fields.map((field: string) => (
                          <span key={field} className="px-2 py-0.5 text-xs bg-muted rounded-full text-muted-foreground">
                            {field}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
          {insightsList.length > 0 ? (
            <div className="px-5 py-3 border-t border-border-light">
              <button
                onClick={() => {
                  const text = insightsList.map((ins) => `【${ins.type === "trend" ? "趋势" : ins.type === "anomaly" ? "异常" : "机会"}】${ins.title}\n${ins.description}`).join("\n\n");
                  handleApplyInsight(text);
                }}
                className="w-full py-2 rounded-[8px] text-[13px] font-medium text-white bg-primary hover:bg-primary-hover"
              >
                应用到文本块
              </button>
            </div>
          ) : null}
        </div>
      </div>
    ) : null}

    {polishModalIdx != null ? (
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <div className="absolute inset-0 bg-black/40" onClick={closePolish} />
        <div className="relative bg-white rounded-lg shadow-xl w-[min(520px,92vw)] max-h-[80vh] flex flex-col">
          <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-foreground">AI 润色</h3>
            <button onClick={closePolish} className="p-1.5 rounded-md hover:bg-muted text-muted-foreground" title="关闭">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="px-5 py-4 overflow-auto flex-1 space-y-4">
            {/* 风格选择 + 开始润色按钮 — 结果出来前显示 */}
            {!polishResult && !polishLoading ? (
              <>
                <div>
                  <label className="block text-[12px] text-muted-foreground mb-1.5">润色风格</label>
                  <select
                    value={polishStyle}
                    onChange={(e) => setPolishStyle(e.target.value)}
                    className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
                  >
                    <option value="professional">专业</option>
                    <option value="casual">轻松</option>
                    <option value="concise">精炼</option>
                    <option value="academic">学术</option>
                  </select>
                </div>
                <button
                  onClick={handleStartPolish}
                  className="flex items-center gap-2 px-4 py-2 rounded-[8px] text-[13px] font-medium text-white bg-ai hover:bg-ai-hover"
                >
                  <Wand2 className="w-4 h-4" />
                  开始润色
                </button>
              </>
            ) : polishLoading ? (
              <div className="flex items-center justify-center py-8 text-[13px] text-muted-foreground">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                AI 正在润色...
              </div>
            ) : polishError ? (
              <div className="px-3 py-2 rounded-md bg-danger-light text-danger text-[13px]">
                {polishError}
                <button
                  onClick={handleStartPolish}
                  className="ml-3 underline text-ai hover:text-ai-hover"
                >
                  重试
                </button>
              </div>
            ) : polishResult ? (
              <>
                <div>
                  <div className="text-[11px] font-medium text-muted-foreground mb-1">润色风格</div>
                  <span className="text-[13px] text-card-foreground">
                    {polishStyle === "professional" ? "专业" : polishStyle === "casual" ? "轻松" : polishStyle === "concise" ? "精炼" : "学术"}
                  </span>
                </div>
                <div>
                  <div className="text-[11px] font-medium text-muted-foreground mb-1">原文</div>
                  <div className="p-3 rounded-[8px] bg-muted text-[13px] text-card-foreground leading-relaxed">
                    {polishResult.original}
                  </div>
                </div>
                <div>
                  <div className="text-[11px] font-medium text-ai mb-1">润色后</div>
                  <div className="p-3 rounded-[8px] bg-ai-light text-[13px] text-foreground leading-relaxed border border-ai">
                    {polishResult.polished}
                  </div>
                </div>
              </>
            ) : null}
          </div>
          {polishResult ? (
            <div className="px-5 py-3 border-t border-border-light flex items-center justify-end gap-2">
              <button
                onClick={handleApplyPolish}
                className="px-4 py-2 rounded-[8px] text-[13px] font-medium text-white bg-primary hover:bg-primary-hover"
              >
                应用
              </button>
            </div>
          ) : null}
        </div>
      </div>
    ) : null}
  </>
  );
}