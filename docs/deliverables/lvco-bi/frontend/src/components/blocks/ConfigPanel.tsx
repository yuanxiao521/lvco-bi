import { useState } from "react";
import {
  BarChart2,
  BarChart3,
  Layers,
  Gauge,
  LineChart as LineChartIcon,
  PieChart as PieChartIcon,
  ScatterChart as ScatterChartIcon,
  AreaChart as AreaChartIcon,
  Circle,
  Funnel,
  Radar,
  Grid3x3,
  Network,
  X,
  Calendar,
  Hash,
  Type,
  Sparkles,
  Loader2,
  Palette,
  ChevronDown,
  ChevronUp,
  ChevronLeft,
  ChevronRight,
  AlignJustify,
} from "lucide-react";
import type { ChartType, FilterConfig, MeasureConfig } from "../../api/types";
import { recommendChartTypes } from "../../api/canvases";
import {
  decodeDraggedField,
  FIELD_DRAG_MIME,
} from "./FieldPanel";
import type { SchemaField } from "../../api/types";
import { PALETTE_PRESETS, CHART_TYPE_LABELS } from "../../types/canvas";

interface ConfigPanelProps {
  chartType: ChartType;
  onChartTypeChange: (t: ChartType) => void;
  dimensions: string[];
  measures: MeasureConfig[];
  filters: FilterConfig[];
  onRemoveDimension: (i: number) => void;
  onRemoveMeasure: (i: number) => void;
  onRemoveFilter: (i: number) => void;
  onChangeMeasureAgg: (index: number, agg: MeasureConfig["agg"]) => void;
  onApply: () => void;
  onReset: () => void;
  applying?: boolean;
  canvasId?: string | null;
  renderer?: string;
  onRendererChange?: (r: string) => void;
  applyMode?: "create" | "update";
  onClearSelection?: () => void;
  onDropField?: (payload: { name: string; category: SchemaField["category"] }) => void;
  palette?: string;
  onPaletteChange?: (id: string) => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  /** AI 推荐图表时设置维度 */
  onSetDimensions?: (dims: string[]) => void;
  /** AI 推荐图表时设置度量 */
  onSetMeasures?: (meas: MeasureConfig[]) => void;
  /** 数据源 ID（AI 推荐时用于在没有 canvasId 时调用 datasource 端点） */
  datasourceId?: string | null;
}

const CHART_TYPE_LABELS_LOCAL: Record<string, string> = {
  bar: "柱状图",
  line: "折线图",
  pie: "饼图",
  donut: "环形图",
  area: "面积图",
  scatter: "散点图",
  funnel: "漏斗图",
  heatmap: "热力图",
  radar: "雷达图",
  sankey: "桑基图",
  grouped_bar: "分组柱状图",
  stacked_bar: "堆叠柱状图",
  horizontal_bar: "水平条形图",
  kpi_card: "KPI 卡片",
};

function chartTypeLabel(t: string): string {
  return CHART_TYPE_LABELS_LOCAL[t] ?? CHART_TYPE_LABELS[t as keyof typeof CHART_TYPE_LABELS] ?? t;
}

const CHART_TYPES: Array<{ value: ChartType; icon: typeof BarChart2; label: string }> = [
  { value: "bar", icon: BarChart2, label: "柱状图" },
  { value: "line", icon: LineChartIcon, label: "折线图" },
  { value: "pie", icon: PieChartIcon, label: "饼图" },
  { value: "donut", icon: Circle, label: "环形图" },
  { value: "scatter", icon: ScatterChartIcon, label: "散点图" },
  { value: "area", icon: AreaChartIcon, label: "面积图" },
  { value: "funnel", icon: Funnel, label: "漏斗图" },
  { value: "heatmap", icon: Grid3x3, label: "热力图" },
  { value: "radar", icon: Radar, label: "雷达图" },
  { value: "sankey", icon: Network, label: "桑基图" },
  { value: "grouped_bar", icon: BarChart3, label: "分组柱状图" },
  { value: "stacked_bar", icon: Layers, label: "堆叠柱状图" },
  { value: "horizontal_bar", icon: AlignJustify, label: "水平条形图" },
  { value: "kpi_card", icon: Gauge, label: "KPI 卡片" },
];

function DropZone<T extends { id: string; label: string }>({
  label,
  empty,
  badgeIcon,
  badgeClass,
  items,
  renderItem,
  onRemove,
  onDropField,
  acceptCategories,
}: {
  label: string;
  empty: string;
  badgeIcon: React.ReactNode;
  badgeClass: string;
  items: T[];
  renderItem?: (item: T) => React.ReactNode;
  onRemove: (id: string) => void;
  onDropField?: (payload: { name: string; category: SchemaField["category"] }) => void;
  acceptCategories?: SchemaField["category"][];
}) {
  const [dragOver, setDragOver] = useState(false);
  return (
    <div>
      <div className="text-[11px] font-medium mb-1.5 text-muted-foreground">
        {label}
      </div>
      <div
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes(FIELD_DRAG_MIME) || e.dataTransfer.types.includes("text/plain")) {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
            setDragOver(true);
          }
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          let payload = decodeDraggedField(e.dataTransfer.getData(FIELD_DRAG_MIME));
          if (!payload) {
            payload = decodeDraggedField(e.dataTransfer.getData("text/plain"));
          }
          if (!payload || !onDropField) return;
          if (acceptCategories && !acceptCategories.includes(payload.category)) return;
          onDropField({ name: payload.name, category: payload.category });
        }}
        className={`border border-dashed rounded-[8px] p-2.5 min-h-[44px] flex flex-wrap gap-1.5 transition-colors ${
          dragOver
            ? "border-primary bg-primary-light/40"
            : "border-border"
        }`}
      >
        {items.length === 0 ? (
          <span className="text-[11px] text-muted-foreground self-center">
            {empty}
          </span>
        ) : (
          items.map((item) => (
            <span
              key={item.id}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-[6px] text-[11px] font-medium ${badgeClass}`}
            >
              <span className="w-3 h-3 rounded text-[8px] font-bold flex items-center justify-center">
                {badgeIcon}
              </span>
              {renderItem ? renderItem(item) : item.label}
              <X
                className="w-3 h-3 cursor-pointer opacity-60 hover:opacity-100"
                onClick={() => onRemove(item.id)}
              />
            </span>
          ))
        )}
      </div>
    </div>
  );
}

export default function ConfigPanel({
  chartType,
  onChartTypeChange,
  dimensions,
  measures,
  filters,
  onRemoveDimension,
  onRemoveMeasure,
  onRemoveFilter,
  onChangeMeasureAgg,
  onApply,
  onReset,
  applying,
  canvasId,
  renderer,
  onRendererChange,
  applyMode = "create",
  onClearSelection,
  onDropField,
  palette = "default",
  onPaletteChange,
  collapsed = false,
  onToggleCollapsed,
  onSetDimensions,
  onSetMeasures,
  datasourceId,
}: ConfigPanelProps) {
  const [showRecommend, setShowRecommend] = useState(false);
  const [chartSettingsOpen, setChartSettingsOpen] = useState(true);
  const [recommendLoading, setRecommendLoading] = useState(false);
  const [recommendError, setRecommendError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<
    Array<{ chart_type: string; rationale: string; config: Record<string, unknown>; confidence: number }>
  >([]);

  const handleRecommend = async () => {
    if (measures.length === 0) {
      setRecommendError("请先添加至少一个度量");
      setSuggestions([]);
      setShowRecommend(true);
      return;
    }
    setRecommendLoading(true);
    setRecommendError(null);
    try {
      const currentConfig = {
        dimensions,
        measures,
        filters,
        chartType,
        limit: 20,
      };
      const result = await recommendChartTypes(
        canvasId,
        currentConfig as unknown as Record<string, unknown>,
        datasourceId ?? undefined
      );
      setSuggestions(result.suggestions);
      setShowRecommend(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "AI 推荐失败";
      setRecommendError(msg);
      setSuggestions([]);
      setShowRecommend(true);
    } finally {
      setRecommendLoading(false);
    }
  };

  const handleApplySuggestion = (config: Record<string, unknown>) => {
    // 应用图表类型（兼容 camelCase 和 snake_case）
    const newChartType =
      (typeof config.chartType === "string" && config.chartType) ||
      (typeof config.chart_type === "string" && config.chart_type) ||
      null;
    if (newChartType) {
      onChartTypeChange(newChartType as ChartType);
    }
    // 应用维度
    if (Array.isArray(config.dimensions)) {
      const dims = (config.dimensions as unknown[])
        .map((d) => (typeof d === "string" ? d : (d as { field?: string; name?: string })?.field || (d as { name?: string })?.name))
        .filter((d): d is string => typeof d === "string" && d.length > 0);
      if (dims.length > 0) {
        onSetDimensions?.(dims);
      }
    }
    // 应用度量
    if (Array.isArray(config.measures)) {
      const meas: MeasureConfig[] = (config.measures as unknown[])
        .map((m) => {
          if (typeof m === "string") return { field: m, agg: "SUM" as const };
          const obj = m as { field?: string; name?: string; agg?: string };
          const field = obj.field || obj.name;
          if (!field) return null;
          const agg = (obj.agg as MeasureConfig["agg"]) || "SUM";
          return { field, agg };
        })
        .filter((m): m is MeasureConfig => m !== null);
      if (meas.length > 0) {
        onSetMeasures?.(meas);
      }
    }
    setShowRecommend(false);
    // 等待 React 状态提交后再触发 apply，避免读到旧 dimensions/measures
    setTimeout(() => onApply(), 150);
  };

  return (
    <div
      className={`flex-shrink-0 bg-white border-l border-border-light flex flex-col overflow-hidden transition-[width] duration-200 ${
        collapsed ? "w-[44px]" : "w-[260px]"
      }`}
    >
      <div className="px-4 py-3 border-b border-border-light flex items-center justify-between gap-2">
        {collapsed ? (
          <button
            onClick={onToggleCollapsed}
            title="展开图表配置"
            className="w-full h-7 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground hover:text-primary transition-colors active:scale-95"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
        ) : (
          <>
            <span className="text-[13px] font-semibold text-foreground">
              图表配置
            </span>
            <button
              onClick={onToggleCollapsed}
              title="收起图表配置"
              className="w-7 h-7 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground hover:text-primary transition-colors active:scale-95"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </>
        )}
      </div>

      {collapsed ? null : (
        <div className="flex-1 px-4 py-3 space-y-4 overflow-y-auto overflow-x-hidden no-scrollbar">
        {/* 图表设置 - 可折叠 */}
        <div>
          <button
            onClick={() => setChartSettingsOpen(!chartSettingsOpen)}
            className="w-full flex items-center justify-between text-[11px] font-medium mb-2 text-muted-foreground hover:text-card-foreground transition-colors"
          >
            <span>图表设置</span>
            {chartSettingsOpen ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          </button>
          {chartSettingsOpen ? (
            <div className="space-y-3">
              <div>
                <div className="text-[11px] font-medium mb-2 text-muted-foreground">
                  图表类型
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {CHART_TYPES.map(({ value, icon: Icon, label }) => (
                    <button
                      key={value}
                      onClick={() => onChartTypeChange(value)}
                      className={`flex flex-col items-center gap-1 p-2 rounded-[8px] border text-[10px] transition-colors ${
                        chartType === value
                          ? "border-primary bg-primary-light text-primary font-medium"
                          : "border-border text-muted-foreground hover:border-primary"
                      }`}
                    >
                      <Icon className="w-4 h-4" />
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <div className="text-[11px] font-medium mb-2 text-muted-foreground">
                  渲染器
                </div>
                <select
                  value={renderer || "recharts"}
                  onChange={(e) => onRendererChange?.(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
                >
                  <option value="recharts">Recharts</option>
                  <option value="echarts">ECharts</option>
                </select>
              </div>
              <div>
                <div className="text-[11px] font-medium mb-2 text-muted-foreground flex items-center gap-1">
                  <Palette className="w-3 h-3" />
                  配色方案
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  {PALETTE_PRESETS.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => onPaletteChange?.(p.id)}
                      className={`flex items-center gap-2 px-2 py-1.5 rounded-[6px] border text-[11px] transition-colors ${
                        palette === p.id
                          ? "border-primary bg-primary-light"
                          : "border-border hover:border-primary/50"
                      }`}
                      title={p.name}
                    >
                      <div className="flex gap-0.5 flex-shrink-0">
                        {p.colors.slice(0, 4).map((c, i) => (
                          <span
                            key={i}
                            className="w-3 h-3 rounded-[2px]"
                            style={{ background: c }}
                          />
                        ))}
                      </div>
                      <span className="text-card-foreground truncate">{p.name}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : null}
        </div>

        <DropZone
          label="维度"
          empty="拖拽维度字段到此处"
          badgeIcon="A"
          badgeClass="bg-success-light text-success"
          items={dimensions.map((d) => ({ id: d, label: d }))}
          onRemove={(id) => onRemoveDimension(dimensions.indexOf(id))}
          onDropField={onDropField}
          acceptCategories={["dimension", "key"]}
        />

        <DropZone
          label="度量"
          empty="拖拽度量字段到此处"
          badgeIcon="#"
          badgeClass="bg-info-light text-info"
          items={measures.map((m, i) => ({ id: `${m.field}-${i}`, label: m.field, index: i }))}
          renderItem={(item) => {
            const measure = measures[item.index];
            return (
              <span className="inline-flex items-center gap-1">
                {measure.field}
                <select
                  value={measure.agg}
                  onChange={(e) =>
                    onChangeMeasureAgg(item.index, e.target.value as MeasureConfig["agg"])
                  }
                  className="text-[9px] bg-transparent border-none outline-none cursor-pointer text-info"
                >
                  <option>SUM</option>
                  <option>AVG</option>
                  <option>MAX</option>
                  <option>MIN</option>
                  <option>COUNT</option>
                  <option>STDDEV</option>
                  <option>MEDIAN</option>
                  <option>COUNT_DISTINCT</option>
                </select>
              </span>
            );
          }}
          onRemove={(id) => {
            const idx = measures.findIndex(
              (_, i) => `${measures[i].field}-${i}` === id
            );
            if (idx >= 0) onRemoveMeasure(idx);
          }}
          onDropField={onDropField}
          acceptCategories={["measure"]}
        />

        <DropZone
          label="时间筛选"
          empty="拖拽时间字段到此处"
          badgeIcon={<Calendar className="w-2 h-2" />}
          badgeClass="bg-[#F3F0FF] text-chart-6"
          items={filters
            .filter((f) => f.op === "between")
            .map((f, i) => ({ id: `${f.field}-${i}`, label: f.field }))}
          onRemove={(id) => {
            const idx = filters.findIndex(
              (_, i) => `${filters[i].field}-${i}` === id
            );
            if (idx >= 0) onRemoveFilter(idx);
          }}
          onDropField={onDropField}
          acceptCategories={["time"]}
        />

        {/* AI 推荐图表 - 内联在配置面板内 */}
        <div>
          <button
            onClick={handleRecommend}
            disabled={recommendLoading || measures.length === 0}
            title={measures.length === 0 ? "请先添加至少一个度量" : undefined}
            className="flex items-center gap-1.5 w-full py-2 rounded-[8px] text-[12px] font-medium border border-ai text-ai bg-white hover:bg-ai-light disabled:opacity-50 disabled:cursor-not-allowed justify-center"
          >
            {recommendLoading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Sparkles className="w-3.5 h-3.5" />
            )}
            AI 推荐图表
          </button>
          {measures.length === 0 ? (
            <p className="mt-1 text-[10px] text-muted-foreground leading-relaxed">
              请先在下方拖入至少一个度量字段，再让 AI 推荐合适的图表
            </p>
          ) : null}

          {showRecommend ? (
            <div className="mt-2 rounded-[8px] border border-border-light bg-white overflow-hidden">
              <div className="px-2.5 py-1.5 border-b border-border-light flex items-center justify-between">
                <span className="text-[11px] font-semibold text-foreground flex items-center gap-1">
                  <Sparkles className="w-3 h-3 text-ai" />
                  推荐图表
                </span>
                <X
                  className="w-3 h-3 text-muted-foreground cursor-pointer"
                  onClick={() => setShowRecommend(false)}
                />
              </div>
              <div className="p-1.5 space-y-1.5 max-h-[280px] overflow-y-auto">
                {recommendError ? (
                  <div className="px-2 py-1.5 text-[11px] text-danger">
                    {recommendError}
                  </div>
                ) : suggestions.length === 0 ? (
                  <div className="px-2 py-3 text-center text-[11px] text-muted-foreground">
                    {recommendLoading ? "AI 分析中..." : "暂无推荐"}
                  </div>
                ) : (
                  suggestions.map((s, i) => (
                    <div
                      key={i}
                      className="p-2 rounded-[6px] border border-border-light hover:border-primary transition-colors"
                    >
                      <div className="flex items-center justify-between mb-0.5">
                        <span className="text-[11px] font-semibold text-foreground">
                          {chartTypeLabel(s.chart_type)}
                        </span>
                        {s.confidence != null ? (
                          <span className="text-[10px] text-muted-foreground">
                            {(s.confidence * 100).toFixed(0)}%
                          </span>
                        ) : null}
                      </div>
                      <p className="text-[10px] text-muted-foreground mb-1.5 leading-relaxed">
                        {s.rationale}
                      </p>
                      <button
                        onClick={() => handleApplySuggestion(s.config)}
                        className="w-full py-1 rounded-[4px] text-[10px] font-medium text-white bg-primary hover:bg-primary-hover"
                      >
                        套用
                      </button>
                    </div>
                  ))
                )}
              </div>
            </div>
          ) : null}
        </div>

        <div className="pt-2">
          {applyMode === "update" ? (
            <div className="mb-2 px-2.5 py-1.5 rounded-[6px] bg-primary-light text-primary text-[11px] flex items-center justify-between">
              <span>将更新当前选中的图表</span>
              {onClearSelection ? (
                <button
                  onClick={onClearSelection}
                  className="text-primary hover:underline text-[11px]"
                >
                  改为新建
                </button>
              ) : null}
            </div>
          ) : null}
          <div className="flex gap-2">
            <button
              onClick={onApply}
              disabled={applying}
              className="flex-1 py-2 rounded-[8px] text-[12px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50"
            >
              {applying
                ? "应用中..."
                : applyMode === "update"
                ? "更新图表"
                : "新增图表"}
            </button>
            <button
              onClick={onReset}
              className="flex-1 py-2 rounded-[8px] text-[12px] font-medium border border-border text-card-foreground bg-card hover:bg-muted"
            >
              重置
            </button>
          </div>
        </div>
      </div>
      )}

      {/* AI 推荐图表已改为内联展示，不再需要 Portal 浮窗 */}
    </div>
  );
}

export { Hash, Type };
