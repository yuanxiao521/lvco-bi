import { useEffect, useState } from "react";
import {
  GripVertical,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Calendar,
  Loader2,
} from "lucide-react";
import { listDatasources, getDatasource } from "../../api/datasources";
import type { DataSource, SchemaField } from "../../api/types";

interface FieldPanelProps {
  selectedDatasourceId: string | null;
  onSelectDatasource: (id: string) => void;
  onAddDimension: (field: string) => void;
  onAddMeasure: (field: string) => void;
  onAddFilter: (field: string) => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}

const TYPE_BADGE: Record<SchemaField["category"], { color: string; label: string }> = {
  key: { color: "bg-muted text-muted-foreground", label: "K" },
  measure: { color: "bg-info text-white", label: "#" },
  dimension: { color: "bg-success text-white", label: "A" },
  time: { color: "bg-chart-6 text-white", label: "T" },
};

export const FIELD_DRAG_MIME = "application/x-lvco-field";

export interface DraggedFieldPayload {
  name: string;
  category: SchemaField["category"];
  displayName?: string;
}

export function encodeDraggedField(payload: DraggedFieldPayload): string {
  return JSON.stringify(payload);
}

export function decodeDraggedField(raw: string | null): DraggedFieldPayload | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      typeof parsed.name === "string" &&
      typeof parsed.category === "string"
    ) {
      return parsed as DraggedFieldPayload;
    }
  } catch {
    // fall through
  }
  return null;
}

export default function FieldPanel({
  selectedDatasourceId,
  onSelectDatasource,
  onAddDimension,
  onAddMeasure,
  onAddFilter,
  collapsed = false,
  onToggleCollapsed,
}: FieldPanelProps) {
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [fields, setFields] = useState<SchemaField[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingFields, setLoadingFields] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const res = await listDatasources({ pageSize: 100 });
        if (!cancelled) setDatasources(res.items);
      } catch {
        if (!cancelled) setDatasources([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedDatasourceId) {
      setFields([]);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoadingFields(true);
      try {
        const ds = await getDatasource(selectedDatasourceId);
        if (!cancelled) setFields(ds.schemaMeta?.fields ?? []);
      } catch {
        if (!cancelled) setFields([]);
      } finally {
        if (!cancelled) setLoadingFields(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedDatasourceId]);

  const grouped = {
    measure: fields.filter((f) => f.category === "measure"),
    dimension: fields.filter((f) => f.category === "dimension"),
    time: fields.filter((f) => f.category === "time"),
    key: fields.filter((f) => f.category === "key"),
  };

  return (
    <div
      className={`flex-shrink-0 bg-white border-r border-border-light flex flex-col overflow-hidden transition-[width] duration-200 ${
        collapsed ? "w-[44px]" : "w-[240px]"
      }`}
    >
      <div className="px-3 py-3 border-b border-border-light flex items-center justify-between gap-2">
        {collapsed ? (
          <button
            onClick={onToggleCollapsed}
            title="展开字段列表"
            className="w-full h-7 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground hover:text-primary transition-colors active:scale-95"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        ) : (
          <>
            <div className="text-[13px] font-semibold text-foreground">
              字段列表
            </div>
            <button
              onClick={onToggleCollapsed}
              title="收起字段列表"
              className="w-7 h-7 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground hover:text-primary transition-colors active:scale-95"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
          </>
        )}
      </div>
      {!collapsed ? (
        <div className="px-4 pb-3 border-b border-border-light">
          <div className="relative">
            <select
              value={selectedDatasourceId ?? ""}
              onChange={(e) => onSelectDatasource(e.target.value)}
              disabled={loading}
              className="w-full appearance-none flex items-center justify-between px-2.5 py-1.5 rounded-[6px] border border-border text-[12px] bg-card text-card-foreground focus:outline-none focus:border-primary cursor-pointer"
            >
              <option value="" disabled>
                {loading ? "加载中..." : "选择数据源"}
              </option>
              {datasources.map((ds) => (
                <option key={ds.id} value={ds.id}>
                  {ds.name}
                </option>
              ))}
            </select>
            <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-muted-foreground pointer-events-none" />
          </div>
        </div>
      ) : null}

      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-0.5 no-scrollbar">
        {loadingFields ? (
          <div className="flex items-center justify-center py-8 text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
          </div>
        ) : !selectedDatasourceId ? (
          <div className="text-[11px] text-muted-foreground text-center py-8">
            请先选择数据源
          </div>
        ) : fields.length === 0 ? (
          <div className="text-[11px] text-muted-foreground text-center py-8">
            该数据源暂无字段
          </div>
        ) : (
          <>
            {(["measure", "dimension", "time", "key"] as const).map((cat) => {
              const list = grouped[cat];
              if (list.length === 0) return null;
              const labels: Record<typeof cat, string> = {
                measure: "度量",
                dimension: "维度",
                time: "时间",
                key: "主键",
              };
              return (
                <div key={cat}>
                  <div className="text-[10px] font-semibold uppercase tracking-wider px-1 py-1.5 text-muted-foreground">
                    {labels[cat]}
                  </div>
                  {list.map((f) => {
                    const badge = TYPE_BADGE[f.category];
                    const handleClick = () => {
                      if (f.category === "measure") onAddMeasure(f.name);
                      else if (f.category === "time") onAddDimension(f.name);
                      else onAddDimension(f.name);
                    };
                    return (
                      <div
                        key={f.name}
                        draggable={true}
                        onDragStart={(e) => {
                          const payload: DraggedFieldPayload = {
                            name: f.name,
                            category: f.category,
                            displayName: f.displayName,
                          };
                          const encoded = encodeDraggedField(payload);
                          e.dataTransfer.setData(FIELD_DRAG_MIME, encoded);
                          e.dataTransfer.setData("text/plain", encoded);
                          e.dataTransfer.effectAllowed = "copy";
                        }}
                        onClick={handleClick}
                        className="flex items-center gap-2 px-2 py-1.5 rounded-[6px] hover:bg-muted cursor-grab active:cursor-grabbing group"
                      >
                        <GripVertical className="w-3 h-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                        <span
                          className={`w-4 h-4 rounded text-[10px] font-bold flex items-center justify-center ${badge.color}`}
                        >
                          {f.category === "time" ? (
                            <Calendar className="w-2.5 h-2.5" />
                          ) : (
                            badge.label
                          )}
                        </span>
                        <span className="text-[12px] text-card-foreground truncate">
                          {f.displayName || f.name}
                        </span>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
