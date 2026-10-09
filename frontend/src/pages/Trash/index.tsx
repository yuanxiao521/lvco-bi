import { useEffect, useCallback, useState, useMemo } from "react";
import {
  Trash2,
  Loader2,
  RefreshCw,
  FileText,
  Gauge,
  CheckSquare,
  Square,
  MinusSquare,
  Lock,
} from "lucide-react";
import { listTrash, restoreTrashItem, permanentDeleteTrashItem } from "../../api/trash";
import type { TrashItem } from "../../api/trash";
import { useToast } from "../../components/ui/Toast";
import { useAuthStore } from "../../stores/authStore";

const TYPE_META: Record<string, { label: string; icon: typeof FileText; color: string }> = {
  canvas: { label: "画布", icon: FileText, color: "bg-info-light text-info" },
  dashboard: { label: "仪表盘", icon: Gauge, color: "bg-primary-light text-primary" },
  report: { label: "报表", icon: FileText, color: "bg-warning-light text-warning" },
};

const itemKey = (it: TrashItem) => `${it.type}-${it.id}`;

export default function TrashPage() {
  const toast = useToast();
  const currentUser = useAuthStore((s) => s.user);
  const isAdmin = currentUser?.role === "admin";
  const [items, setItems] = useState<TrashItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchLoading, setBatchLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listTrash();
      setItems(res.items ?? []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 列表变化时清理已不存在的选中项
  useEffect(() => {
    setSelected((prev) => {
      const valid = new Set(items.map(itemKey));
      const next = new Set<string>();
      prev.forEach((k) => { if (valid.has(k)) next.add(k); });
      return next;
    });
  }, [items]);

  const allKeys = useMemo(() => items.map(itemKey), [items]);
  const allSelected = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
  const someSelected = allKeys.some((k) => selected.has(k)) && !allSelected;
  const selectedCount = selected.size;

  const toggleAll = () => {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(allKeys));
    }
  };

  const toggleOne = (it: TrashItem) => {
    const k = itemKey(it);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const handleRestore = async (item: TrashItem) => {
    setActionLoading(itemKey(item));
    try {
      await restoreTrashItem(item.type, item.id);
      setItems((prev) => prev.filter((i) => !(i.id === item.id && i.type === item.type)));
      toast.success(`已恢复「${item.title}」`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "恢复失败";
      toast.error(msg);
    } finally {
      setActionLoading(null);
    }
  };

  const handlePermanentDelete = async (item: TrashItem) => {
    if (!isAdmin) {
      toast.error("彻底删除仅限管理员操作");
      return;
    }
    const ok = await toast.confirm(
      `确定彻底删除「${item.title}」？此操作不可恢复。`,
    );
    if (!ok) return;
    setActionLoading(itemKey(item));
    try {
      await permanentDeleteTrashItem(item.type, item.id);
      setItems((prev) => prev.filter((i) => !(i.id === item.id && i.type === item.type)));
      toast.success(`已彻底删除「${item.title}」`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "删除失败";
      toast.error(msg);
    } finally {
      setActionLoading(null);
    }
  };

  // 批量操作：取并集 intersection（只处理当前选中的；过程中列表可能变）
  const getSelectedItems = (): TrashItem[] => items.filter((it) => selected.has(itemKey(it)));

  const handleBatchRestore = async () => {
    const targets = getSelectedItems();
    if (targets.length === 0) return;
    const ok = await toast.confirm(
      `确定要恢复选中的 ${targets.length} 项吗？`,
    );
    if (!ok) return;

    setBatchLoading(true);
    try {
      const results = await Promise.allSettled(
        targets.map((t) => restoreTrashItem(t.type, t.id)),
      );
      const successCount = results.filter((r) => r.status === "fulfilled").length;
      const failCount = results.length - successCount;
      // 从列表中移除成功的
      const successKeys = new Set(
        targets
          .filter((_, i) => results[i].status === "fulfilled")
          .map(itemKey),
      );
      setItems((prev) => prev.filter((it) => !successKeys.has(itemKey(it))));
      setSelected(new Set());
      if (failCount === 0) {
        toast.success(`已恢复 ${successCount} 项`);
      } else {
        toast.warning(`成功 ${successCount} 项，失败 ${failCount} 项`);
      }
    } finally {
      setBatchLoading(false);
    }
  };

  const handleBatchDelete = async () => {
    const targets = getSelectedItems();
    if (targets.length === 0) return;
    if (!isAdmin) {
      toast.error("彻底删除仅限管理员操作");
      return;
    }
    const ok = await toast.confirm(
      `确定要彻底删除选中的 ${targets.length} 项吗？此操作不可恢复。`,
    );
    if (!ok) return;

    setBatchLoading(true);
    try {
      const results = await Promise.allSettled(
        targets.map((t) => permanentDeleteTrashItem(t.type, t.id)),
      );
      const successCount = results.filter((r) => r.status === "fulfilled").length;
      const failCount = results.length - successCount;
      const successKeys = new Set(
        targets
          .filter((_, i) => results[i].status === "fulfilled")
          .map(itemKey),
      );
      setItems((prev) => prev.filter((it) => !successKeys.has(itemKey(it))));
      setSelected(new Set());
      if (failCount === 0) {
        toast.success(`已彻底删除 ${successCount} 项`);
      } else {
        toast.warning(`成功 ${successCount} 项，失败 ${failCount} 项`);
      }
    } finally {
      setBatchLoading(false);
    }
  };

  return (
    <div className="flex-1 p-6 space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[17px] font-semibold text-foreground flex items-center gap-2">
            <Trash2 className="w-5 h-5 text-danger" />
            回收站
          </h1>
          <p className="text-[12px] text-muted-foreground mt-1">
            软删除的画布、仪表盘、报表在此暂存，可恢复或彻底删除
            {!isAdmin && (
              <span className="ml-2 inline-flex items-center gap-1 text-warning">
                <Lock className="w-3 h-3" />
                彻底删除仅限管理员
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* 批量操作工具栏：有选中项时显示 */}
          {selectedCount > 0 && (
            <>
              <span className="text-[12px] text-muted-foreground">
                已选 <b className="text-foreground">{selectedCount}</b> 项
              </span>
              <button
                onClick={handleBatchRestore}
                disabled={batchLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] border border-border rounded-md text-primary hover:bg-primary-light disabled:opacity-50"
              >
                {batchLoading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="w-3.5 h-3.5" />
                )}
                批量恢复
              </button>
              <button
                onClick={handleBatchDelete}
                disabled={batchLoading || !isAdmin}
                title={!isAdmin ? "仅管理员可彻底删除" : undefined}
                className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] border border-danger rounded-md text-danger hover:bg-danger-light disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {batchLoading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Trash2 className="w-3.5 h-3.5" />
                )}
                批量彻底删除
              </button>
              <button
                onClick={() => setSelected(new Set())}
                className="text-[12px] text-muted-foreground hover:text-foreground px-2"
              >
                取消选择
              </button>
            </>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] border border-border rounded-md text-card-foreground hover:bg-muted disabled:opacity-50"
          >
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <RefreshCw className="w-3.5 h-3.5" />
            )}
            刷新
          </button>
        </div>
      </div>

      {loading && items.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground text-[13px]">
          <Loader2 className="w-4 h-4 animate-spin mr-2" />
          加载中...
        </div>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-[10px] border border-border-light p-16 text-center">
          <Trash2 className="w-14 h-14 mx-auto mb-3 text-muted-foreground/40" />
          <h3 className="text-[14px] font-medium text-foreground mb-1">回收站是空的</h3>
          <p className="text-[12px] text-muted-foreground">
            删除的画布、仪表盘、报表会显示在这里
          </p>
        </div>
      ) : (
        <div className="bg-white rounded-[10px] border border-border-light overflow-hidden">
          <table className="w-full text-[13px]">
            <thead className="bg-muted">
              <tr>
                <th className="w-10 px-4 py-2">
                  <button
                    onClick={toggleAll}
                    className="inline-flex items-center justify-center text-muted-foreground hover:text-foreground"
                    title={allSelected ? "取消全选" : "全选"}
                    aria-pressed={allSelected}
                  >
                    {allSelected ? (
                      <CheckSquare className="w-4 h-4 text-primary" />
                    ) : someSelected ? (
                      <MinusSquare className="w-4 h-4 text-primary" />
                    ) : (
                      <Square className="w-4 h-4" />
                    )}
                  </button>
                </th>
                <th className="text-left px-4 py-2 font-medium text-muted-foreground">类型</th>
                <th className="text-left px-4 py-2 font-medium text-muted-foreground">名称</th>
                <th className="text-left px-4 py-2 font-medium text-muted-foreground">删除时间</th>
                <th className="text-right px-4 py-2 font-medium text-muted-foreground">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const meta = TYPE_META[it.type] ?? { label: it.type, icon: FileText, color: "bg-muted text-muted-foreground" };
                const Icon = meta.icon;
                const key = itemKey(it);
                const isBusy = actionLoading === key;
                const isSelected = selected.has(key);
                return (
                  <tr
                    key={key}
                    className={`border-t border-border-light hover:bg-muted/40 ${isSelected ? "bg-primary-light/30" : ""}`}
                    onClick={(e) => {
                      // 行点击切换选中（按钮区域不触发）
                      const target = e.target as HTMLElement;
                      if (target.closest("button")) return;
                      toggleOne(it);
                    }}
                  >
                    <td className="px-4 py-2">
                      <button
                        onClick={() => toggleOne(it)}
                        className="inline-flex items-center justify-center text-muted-foreground hover:text-foreground"
                      >
                        {isSelected ? (
                          <CheckSquare className="w-4 h-4 text-primary" />
                        ) : (
                          <Square className="w-4 h-4" />
                        )}
                      </button>
                    </td>
                    <td className="px-4 py-2">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium ${meta.color}`}>
                        <Icon className="w-3 h-3" />
                        {meta.label}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-foreground truncate max-w-[400px]">{it.title}</td>
                    <td className="px-4 py-2 text-muted-foreground text-[12px]">
                      {it.deletedAt ? new Date(it.deletedAt).toLocaleString("zh-CN") : "—"}
                    </td>
                    <td className="px-4 py-2 text-right text-[12px]">
                      {isBusy ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin inline" />
                      ) : (
                        <>
                          <button
                            onClick={() => handleRestore(it)}
                            className="text-primary hover:underline mr-3"
                          >
                            恢复
                          </button>
                          <button
                            onClick={() => handlePermanentDelete(it)}
                            className="text-danger hover:underline"
                          >
                            彻底删除
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
