import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, FileText, Loader2, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { deleteCanvas, listCanvases, updateCanvas } from "../../../api/canvases";
import type { Canvas } from "../../../api/types";
import { useToast } from "../../../components/ui/Toast";

/** 画布时间显示：小于7天显示相对时间，否则显示日期 */
function canvasTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
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

interface CanvasListSidebarProps {
  /** 当前打开的画布 ID（高亮显示） */
  currentCanvasId: string | null;
  /** 打开指定画布继续编辑 */
  onOpenCanvas: (id: string) => void;
  /** 新建空白画布 */
  onNewCanvas: () => void;
  /** 删除画布成功的回调（若删除的是当前画布，父组件据此清空工作区） */
  onCanvasDeleted: (id: string) => void;
  /** 外部刷新信号（例如 header 删除当前画布后自增，触发列表重拉） */
  refreshTick?: number;
}

export default function CanvasListSidebar({
  currentCanvasId,
  onOpenCanvas,
  onNewCanvas,
  onCanvasDeleted,
  refreshTick = 0,
}: CanvasListSidebarProps) {
  const toast = useToast();
  const [collapsed, setCollapsed] = useState(false);
  const [canvases, setCanvases] = useState<Canvas[]>([]);
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  // 行内重命名状态（id → 编辑中的标题）
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const res = await listCanvases({ page: 1, pageSize: 50 });
      setCanvases(res.items ?? []);
      setTotal(res.total ?? 0);
    } catch {
      setCanvases([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, refreshTick]);

  const submitRename = async (id: string) => {
    const newTitle = renameValue.trim();
    setRenamingId(null);
    if (!newTitle) {
      refresh();
      return;
    }
    try {
      await updateCanvas(id, { title: newTitle });
      setCanvases((prev) => prev.map((c) => (c.id === id ? { ...c, title: newTitle } : c)));
    } catch {
      toast.error("重命名失败");
      refresh();
    }
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const ok = await toast.confirm("删除后画布将移入回收站，可在回收站中恢复，确定删除吗？");
    if (!ok) return;
    try {
      await deleteCanvas(id);
      setCanvases((prev) => prev.filter((c) => c.id !== id));
      onCanvasDeleted(id);
    } catch {
      toast.error("删除失败");
    }
  };

  const filtered = searchQuery.trim()
    ? canvases.filter((c) => (c.title || "").toLowerCase().includes(searchQuery.trim().toLowerCase()))
    : canvases;

  return (
    <div
      className={`${
        collapsed ? "w-[40px]" : "w-[220px]"
      } flex-shrink-0 hidden md:flex flex-col border-r border-border-light bg-card transition-[width] duration-200`}
    >
      {collapsed ? (
        /* —— 折叠态：窄条，只留展开按钮 —— */
        <div className="flex flex-col items-center py-3 gap-2 flex-1">
          <button
            onClick={() => setCollapsed(false)}
            className="p-1.5 rounded text-muted-foreground hover:text-primary hover:bg-muted transition-colors"
            title="展开画布列表"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
          <FileText className="w-4 h-4 text-muted-foreground/60 mt-2" />
          <span
            className="text-[10px] text-muted-foreground/70"
            style={{ writingMode: "vertical-rl", letterSpacing: "0.2em" }}
          >
            画布
          </span>
        </div>
      ) : (
        <>
          {/* 标题 + 新建 + 折叠按钮 */}
          <div className="flex items-center justify-between px-3 py-3 border-b border-border-light">
            <span className="text-[13px] font-semibold text-foreground">我的画布</span>
            <div className="flex items-center gap-1">
              <button
                onClick={onNewCanvas}
                className="flex items-center gap-1 px-2.5 py-1 rounded-[6px] bg-primary text-white text-[11px] font-medium hover:bg-primary-hover transition-colors"
                title="新建空白画布"
              >
                <Plus className="w-3 h-3" />
                新建
              </button>
              <button
                onClick={() => setCollapsed(true)}
                className="p-1 rounded text-muted-foreground hover:text-primary hover:bg-muted transition-colors"
                title="收起画布列表"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* 搜索框 */}
          <div className="px-3 py-2 border-b border-border-light">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-muted-foreground" />
              <input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索画布..."
                className="w-full pl-7 pr-6 py-1.5 text-[12px] rounded-[6px] bg-muted border border-border text-foreground placeholder:text-muted-foreground outline-none focus:ring-1 focus:ring-ring"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery("")}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>
          </div>

          {/* 画布列表 */}
          <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-4 h-4 text-muted-foreground animate-spin" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-8 text-center text-[12px] text-muted-foreground">
            {searchQuery ? "未找到匹配的画布" : total === 0 ? "暂无画布，点击「新建」开始" : "列表为空"}
          </div>
        ) : (
          filtered.map((c) => {
            const active = c.id === currentCanvasId;
            const renaming = c.id === renamingId;
            return (
              <div
                key={c.id}
                onClick={() => !renaming && onOpenCanvas(c.id)}
                className={`group flex items-center gap-1 px-2 py-2 rounded-[6px] cursor-pointer transition-colors ${
                  active ? "bg-primary-light/40 border border-primary-muted" : "hover:bg-muted border border-transparent"
                }`}
                title={c.title}
              >
                {renaming ? (
                  <input
                    value={renameValue}
                    autoFocus
                    onChange={(e) => setRenameValue(e.target.value)}
                    onBlur={() => submitRename(c.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") submitRename(c.id);
                      if (e.key === "Escape") { setRenamingId(null); setRenameValue(""); }
                    }}
                    onClick={(e) => e.stopPropagation()}
                    className="flex-1 min-w-0 text-[12px] font-medium px-1 py-0.5 rounded border border-primary bg-white outline-none"
                  />
                ) : (
                  <>
                    <FileText className={`w-3.5 h-3.5 flex-shrink-0 ${active ? "text-primary" : "text-muted-foreground"}`} />
                    <div className="flex-1 min-w-0">
                      <p className={`text-[12px] truncate ${active ? "font-medium text-primary" : "text-foreground"}`}>
                        {c.title || "未命名画布"}
                      </p>
                      <p className="text-[10px] text-muted-foreground">
                        更新于 {canvasTime(c.updatedAt ?? c.createdAt)}
                      </p>
                    </div>
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                      <button
                        onClick={(e) => { e.stopPropagation(); setRenamingId(c.id); setRenameValue(c.title || ""); }}
                        className="p-1 rounded hover:bg-border/40 text-muted-foreground hover:text-primary"
                        title="重命名"
                      >
                        <Pencil className="w-3 h-3" />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setRenamingId(null); handleDelete(c.id, e); }}
                        className="p-1 rounded hover:bg-border/40 text-muted-foreground hover:text-destructive"
                        title="删除（进入回收站）"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                    {active && (
                      <span className="w-1.5 h-1.5 rounded-full bg-primary flex-shrink-0" />
                    )}
                  </>
                )}
              </div>
            );
          })
        )}
          </div>
        </>
      )}
    </div>
  );
}