import { useEffect, useRef, useState } from "react";
import {
  GripVertical,
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

const BLOCK_DRAG_MIME = "application/x-lvco-block-reorder";

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
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  const enter = () => {
    setDraft(value);
    setEditing(true);
  };

  const commit = () => {
    setEditing(false);
    if (draft !== value) onCommit(draft);
  };

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
      className={`cursor-text rounded-md hover:bg-muted/60 transition-colors px-1 py-0.5 -mx-1 -my-0.5 ${className ?? ""}`}
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
  draggable?: boolean;
  onDragStart?: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd?: () => void;
  isDragOver?: boolean;
  onDragOver?: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave?: () => void;
  onDrop?: (e: React.DragEvent<HTMLDivElement>) => void;
  children: ReactNode;
  blockWidth?: number;
  onWidthChange?: (w: number) => void;
  blockHeight?: number;
  onHeightChange?: (h: number) => void;
}

function BlockWrapper({
  label,
  labelBg,
  labelText,
  onDelete,
  extraToolbar,
  selected = false,
  onSelect,
  draggable = true,
  onDragStart,
  onDragEnd,
  isDragOver = false,
  onDragOver,
  onDragLeave,
  onDrop,
  children,
  blockWidth,
  onWidthChange,
  blockHeight,
  onHeightChange,
}: BlockWrapperProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const widthDragState = useRef<{ startX: number; startW: number } | null>(null);
  const heightDragState = useRef<{ startY: number; startH: number } | null>(null);

  // 宽度拖拽：用 document 级 mouse 事件，最可靠
  const handleWMouseDown = (e: React.MouseEvent) => {
    if (!onWidthChange) return;
    e.preventDefault();
    e.stopPropagation();
    const currentW = containerRef.current?.offsetWidth ?? 400;
    if (containerRef.current) {
      containerRef.current.style.width = `${currentW}px`;
    }
    widthDragState.current = { startX: e.clientX, startW: currentW };
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

  // 高度拖拽：setPointerCapture 在手柄元素上，操作DOM，最后commit
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
  const handleHPointerUp = (e: React.PointerEvent) => {
    if (!heightDragState.current || !onHeightChange) return;
    (e.currentTarget as HTMLDivElement).releasePointerCapture(e.pointerId);
    const el = containerRef.current;
    const finalH = el ? parseInt(el.style.height || "0") : heightDragState.current.startH;
    onHeightChange(finalH);
    heightDragState.current = null;
  };

  return (
    <div
      ref={containerRef}
      onClick={onSelect}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      style={{
        ...(blockWidth ? { width: `${blockWidth}px` } : { flex: "1 1 auto", minWidth: 240 }),
        ...(blockHeight ? { height: `${blockHeight}px` } : {}),
      }}
      className={`group relative bg-white rounded-[10px] p-5 shadow-card cursor-pointer transition-shadow select-none flex flex-col ${
        blockHeight ? "overflow-hidden" : ""
      } ${
        selected
          ? "ring-2 ring-primary shadow-md"
          : "hover:shadow-md"
      } ${isDragOver ? "ring-2 ring-primary/60 bg-primary-light/30" : ""}`}
    >
      <div
        title="拖动以重排序"
        className="absolute -left-8 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing flex items-center justify-center w-6 h-8 rounded-[6px] hover:bg-muted text-muted-foreground"
      >
        <GripVertical className="w-4 h-4" />
      </div>
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
      <span
        className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium mb-3 ${labelBg} ${labelText}`}
      >
        {label}
      </span>
      {selected ? (
        <span className="ml-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-primary text-white">
          已选中
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
  onReorderBlocks?: (fromIdx: number, toIdx: number) => void;
  selectedBlockIdx?: number | null;
  onSelectBlock?: (index: number | null) => void;
}

function isTextBlock(b: CanvasBlock): b is CanvasBlock & { type: "text" | "h1" | "h2"; content: string } {
  return b.type === "text" || b.type === "h1" || b.type === "h2";
}

function isChartBlock(
  b: CanvasBlock
): b is CanvasBlock & { type: "chart"; blockId: string; title?: string; renderer?: string; palette?: string; height?: number } {
  return b.type === "chart" && typeof (b as { blockId?: unknown }).blockId === "string";
}

function isDividerBlock(b: CanvasBlock): b is CanvasBlock & { type: "divider" } {
  return b.type === "divider";
}

function isImageBlock(b: CanvasBlock): b is CanvasBlock & { type: "image"; src: string; alt?: string; width?: number; height?: number } {
  return b.type === "image";
}

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
  const [mode, setMode] = useState<"upload" | "url">(block.src?.startsWith("http") || block.src?.startsWith("data:") ? (block.src?.startsWith("http") ? "url" : "upload") : "upload");
  const [urlDraft, setUrlDraft] = useState(block.src ?? "");
  const [altDraft, setAltDraft] = useState(block.alt ?? "");
  const [dragOver, setDragOver] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) readFile(f);
    e.target.value = "";
  };

  const onDropFile = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) readFile(f);
  };

  const applyUrl = () => {
    if (urlDraft.trim()) {
      onUpdate({ src: urlDraft.trim(), alt: altDraft || undefined });
    }
  };

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
  onReorderBlocks,
  selectedBlockIdx,
  onSelectBlock,
}: CanvasBlocksProps) {
  const [insightsModalBlockIdx, setInsightsModalBlockIdx] = useState<number | null>(null);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [insightsList, setInsightsList] = useState<
    Array<{ type: string; title: string; description: string; severity: string; related_fields?: string[] }>
  >([]);

  const [polishModalIdx, setPolishModalIdx] = useState<number | null>(null);
  const [polishLoading, setPolishLoading] = useState(false);
  const [polishError, setPolishError] = useState<string | null>(null);
  const [polishResult, setPolishResult] = useState<{
    original: string;
    polished: string;
    style: string;
  } | null>(null);
  const [polishStyle, setPolishStyle] = useState("professional");

  // 块拖拽重排状态
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);
  const isInternalDrag = useRef(false); // 解决浏览器 dragover 不暴露自定义 MIME type

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
      const axiosDetail = e?.response?.data?.detail;
      const msg = typeof axiosDetail === "string" ? axiosDetail : axiosDetail?.message;
      setInsightsError(msg || e?.message || "AI 洞察生成失败");
    } finally {
      setInsightsLoading(false);
    }
  };

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

  const handleOpenPolish = async (blockIdx: number) => {
    const block = blocks[blockIdx];
    if (!isTextBlock(block)) return;
    setPolishModalIdx(blockIdx);
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

  const handleBlockDragStart = (e: React.DragEvent<HTMLDivElement>, idx: number) => {
    e.dataTransfer.setData(BLOCK_DRAG_MIME, String(idx));
    e.dataTransfer.setData("text/plain", String(idx));
    e.dataTransfer.effectAllowed = "move";
    isInternalDrag.current = true;
    setDragIdx(idx);
  };

  const handleBlockDragEnd = () => {
    isInternalDrag.current = false;
    setDragIdx(null);
    setDragOverIdx(null);
  };

  const handleBlockDragOver = (e: React.DragEvent<HTMLDivElement>, idx: number) => {
    if (!isInternalDrag.current) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (dragOverIdx !== idx) setDragOverIdx(idx);
  };

  const handleBlockDragLeave = () => {
    // 仅在真正离开时清除（hover 由 dragover 持续触发）
  };

  const handleBlockDrop = (e: React.DragEvent<HTMLDivElement>, dropIdx: number) => {
    e.preventDefault();
    e.stopPropagation();
    let from = dragIdx;
    if (from == null) {
      const raw = e.dataTransfer.getData(BLOCK_DRAG_MIME);
      const parsed = Number(raw);
      if (!Number.isNaN(parsed)) from = parsed;
    }
    if (from == null || from === dropIdx) {
      isInternalDrag.current = false;
      setDragIdx(null);
      setDragOverIdx(null);
      return;
    }
    onReorderBlocks?.(from, dropIdx);
    isInternalDrag.current = false;
    setDragIdx(null);
    setDragOverIdx(null);
  };

  if (!blocks || blocks.length === 0) {
    return (
      <div className="text-center py-16 text-muted-foreground text-[13px]">
        画布为空，从左侧拖拽字段或添加块开始创建报告
      </div>
    );
  }

  return (
    <>
    <div className="flex flex-wrap gap-3">
      {blocks.map((block, idx) => {
        const onDelete = onDeleteBlock ? () => onDeleteBlock(idx) : undefined;
        const isSelected = selectedBlockIdx === idx;
        const handleSelect = onSelectBlock
          ? () => onSelectBlock(isSelected ? null : idx)
          : undefined;
        const isDragging = dragIdx === idx;
        const isDragOver = dragOverIdx === idx && dragIdx !== idx;
        const blockData = block as Record<string, unknown>;
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
                onDragStart={(e) => handleBlockDragStart(e, idx)}
                onDragEnd={handleBlockDragEnd}
                isDragOver={isDragOver}
                onDragOver={(e) => handleBlockDragOver(e, idx)}
                onDragLeave={handleBlockDragLeave}
                onDrop={(e) => handleBlockDrop(e, idx)}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
              >
                <div className={isDragging ? "opacity-40" : ""}>
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
                onDragStart={(e) => handleBlockDragStart(e, idx)}
                onDragEnd={handleBlockDragEnd}
                isDragOver={isDragOver}
                onDragOver={(e) => handleBlockDragOver(e, idx)}
                onDragLeave={handleBlockDragLeave}
                onDrop={(e) => handleBlockDrop(e, idx)}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
              >
                <div className={isDragging ? "opacity-40" : ""}>
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
              onDragStart={(e) => handleBlockDragStart(e, idx)}
              onDragEnd={handleBlockDragEnd}
              isDragOver={isDragOver}
              onDragOver={(e) => handleBlockDragOver(e, idx)}
              onDragLeave={handleBlockDragLeave}
              onDrop={(e) => handleBlockDrop(e, idx)}
              blockWidth={blockW}
              onWidthChange={onWChange}
              blockHeight={blockH}
              onHeightChange={onHChange}
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
              <div className={isDragging ? "opacity-40" : ""}>
                <EditableText
                  value={block.content}
                  className="text-[14px] leading-relaxed text-card-foreground"
                  onCommit={(next) => onUpdateBlockContent?.(idx, next)}
                />
              </div>
            </BlockWrapper>
          );
        }
        if (isDividerBlock(block)) {
          return (
            <BlockWrapper
              key={idx}
              label="分割线"
              labelBg="bg-muted"
              labelText="text-muted-foreground"
              onDelete={onDelete}
              selected={isSelected}
              onSelect={handleSelect}
              onDragStart={(e) => handleBlockDragStart(e, idx)}
              onDragEnd={handleBlockDragEnd}
              isDragOver={isDragOver}
              onDragOver={(e) => handleBlockDragOver(e, idx)}
              onDragLeave={handleBlockDragLeave}
              onDrop={(e) => handleBlockDrop(e, idx)}
              blockWidth={blockW}
              onWidthChange={onWChange}
              blockHeight={blockH}
              onHeightChange={onHChange}
            >
              <div className={isDragging ? "opacity-40" : ""}>
                <hr className="border-none border-t border-border" />
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
              onDragStart={(e) => handleBlockDragStart(e, idx)}
              onDragEnd={handleBlockDragEnd}
              isDragOver={isDragOver}
              onDragOver={(e) => handleBlockDragOver(e, idx)}
              onDragLeave={handleBlockDragLeave}
              onDrop={(e) => handleBlockDrop(e, idx)}
              blockWidth={blockW}
              onWidthChange={onWChange}
              blockHeight={blockH}
              onHeightChange={onHChange}
            >
              <div className={isDragging ? "opacity-40" : ""}>
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
                onDragStart={(e) => handleBlockDragStart(e, idx)}
                onDragEnd={handleBlockDragEnd}
                isDragOver={isDragOver}
                onDragOver={(e) => handleBlockDragOver(e, idx)}
                onDragLeave={handleBlockDragLeave}
                onDrop={(e) => handleBlockDrop(e, idx)}
                blockWidth={blockW}
                onWidthChange={onWChange}
                blockHeight={blockH}
                onHeightChange={onHChange}
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
                <div className={`${isDragging ? "opacity-40" : ""} flex flex-col min-h-0 ${blockH ? "flex-1" : ""}`}>
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
                  <div className="flex-1 min-h-0" style={blockH ? undefined : { height }}>
                    <ChartRenderer
                      config={config}
                      result={result}
                      loading={loading}
                      renderer={renderer}
                      palette={block.palette}
                    />
                  </div>
                </div>
              </BlockWrapper>
            </LazyChartBlock>
          );
        }
        return null;
      })}
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
            <div>
              <label className="block text-[12px] text-muted-foreground mb-1.5">润色风格</label>
              <select
                value={polishStyle}
                onChange={(e) => setPolishStyle(e.target.value)}
                disabled={polishLoading}
                className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
              >
                <option value="professional">专业</option>
                <option value="casual">轻松</option>
                <option value="concise">精炼</option>
                <option value="academic">学术</option>
              </select>
            </div>

            {polishLoading ? (
              <div className="flex items-center justify-center py-8 text-[13px] text-muted-foreground">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                AI 正在润色...
              </div>
            ) : polishError ? (
              <div className="px-3 py-2 rounded-md bg-danger-light text-danger text-[13px]">{polishError}</div>
            ) : polishResult ? (
              <>
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