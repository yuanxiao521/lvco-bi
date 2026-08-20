import {
  BarChart,
  Bar,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  AreaChart,
  Area,
  ScatterChart,
  Scatter,
  ComposedChart,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import type { ChartQueryConfig, QueryResult } from "../../api/types";
import {
  EChartsBar,
  EChartsLine,
  EChartsPie,
  EChartsArea,
  EChartsScatter,
  EChartsFunnel,
  EChartsRadar,
  EChartsHeatmap,
  EChartsHorizontalBar,
  CHART_TYPE_LABELS,
} from "./index";
import { default as EChartsSankey } from './echarts/EChartsSankey'
import { getPaletteById } from "../../types/canvas";
import type { MeasureFieldInfo } from "./echarts/echartsUtils";

/** 中文类型名（供各种图表容器/卡片显示用） */
function chartTypeCn(t: string): string {
  return (CHART_TYPE_LABELS as Record<string, string>)[t] ?? t;
}

const DEFAULT_COLORS = ["#2BB5A0", "#6C7BF2", "#F5A623", "#EF5B5B", "#4EADFF", "#A78BFA"];

function formatYAxis(value: number): string {
  if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}w`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

interface ChartRendererProps {
  config: ChartQueryConfig | null;
  result: QueryResult | null;
  loading?: boolean;
  renderer?: "recharts" | "echarts";
  palette?: string;
  /** 点击图表维度值回调（用于跨图表联动筛选）；ECharts 渲染时生效 */
  onDimensionClick?: (dimension: string, value: string) => void;
}

/** 后端 result_columns 使用原始字段名（非 SQL 别名），直接返回 field */
function measureToColumnAlias(m: { field: string; agg: string }): string {
  return m.field;
}

export default function ChartRenderer({
  config,
  result,
  loading,
  renderer = "recharts",
  palette,
  onDimensionClick,
}: ChartRendererProps) {
  const COLORS = palette ? getPaletteById(palette) : DEFAULT_COLORS;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
        加载中...
      </div>
    );
  }
  if (!result || result.rows.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
        暂无数据
      </div>
    );
  }

  const chartType = config?.chartType ?? "bar";
  const dim = config?.dimensions[0] ?? result.columns[0];
  const measures = config?.measures ?? [];
  const measureKey =
    measures[0]?.field ?? result.columns.find((c) => c !== dim) ?? "";
  const data = result.rows;

  // 构建多度量信息（用于 ECharts 多度量渲染）
  const measureFields: MeasureFieldInfo[] = measures.map((m) => ({
    field: measureToColumnAlias(m),
    label: `${m.agg}(${m.field})`,
  }));

  // 联动筛选事件绑定：点击柱子/节点/扇区时把维度名+维度值抛给上层
  const onEvents = onDimensionClick
    ? {
        click: (params: unknown) => {
          const p = params as { name?: unknown };
          if (p?.name != null && dim) onDimensionClick(dim, String(p.name));
        },
      }
    : undefined;

  // --- ECharts 渲染 ---
  if (renderer === "echarts") {
    // 饼图始终用单度量
    if (chartType === "pie" || chartType === "donut") {
      return (
        <EChartsPie
          data={data}
          nameField={dim}
          valueField={measureFields[0]?.field ?? measureKey}
          colors={COLORS}
          innerRadius={chartType === "donut" ? 80 : undefined}
          onEvents={onEvents}
        />
      );
    }

    // 漏斗图（单度量 + 维度）
    if (chartType === "funnel") {
      return (
        <EChartsFunnel
          data={data}
          nameField={dim}
          valueField={measureFields[0]?.field ?? measureKey}
          colors={COLORS}
          onEvents={onEvents}
        />
      );
    }

    // 雷达图（多度量：每个度量作为一层雷达）
    if (chartType === "radar") {
      return (
        <EChartsRadar
          data={data}
          nameField={dim}
          measureFields={measureFields.length > 0 ? measureFields : [{ field: measureKey, label: measureKey }]}
          colors={COLORS}
          onEvents={onEvents}
        />
      );
    }

    // 热力图（需要 2 个维度 + 1 个度量）
    if (chartType === "heatmap") {
      const dimX = config?.dimensions[0];
      const dimY = config?.dimensions[1];
      const measureField = measureFields[0]?.field ?? measureKey;
      if (dimX && dimY) {
        const xVals: string[] = [];
        const yVals: string[] = [];
        const xSeen = new Set<string>();
        const ySeen = new Set<string>();
        for (const row of data) {
          const xv = String(row[dimX] ?? "");
          const yv = String(row[dimY] ?? "");
          if (xv && !xSeen.has(xv)) { xSeen.add(xv); xVals.push(xv); }
          if (yv && !ySeen.has(yv)) { ySeen.add(yv); yVals.push(yv); }
        }
        if (xVals.length === 0 || yVals.length === 0) {
          return (
            <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
              数据中未找到有效的维度值，请检查字段配置
            </div>
          );
        }
        const matrix: number[][] = yVals.map(() => xVals.map(() => 0));
        for (const row of data) {
          const xv = String(row[dimX] ?? "");
          const yv = String(row[dimY] ?? "");
          const xi = xVals.indexOf(xv);
          const yi = yVals.indexOf(yv);
          if (xi >= 0 && yi >= 0) {
            matrix[yi][xi] = Number(row[measureField]) || 0;
          }
        }
        return <EChartsHeatmap xFields={xVals} yFields={yVals} matrix={matrix} xLabel={dimX} yLabel={dimY} palette={COLORS} onEvents={onEvents} />;
      }
      return (
        <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
          热力图需要 2 个维度 + 1 个度量，请在左侧字段面板追加维度
        </div>
      );
    }

    // 桑基图
    if (chartType === "sankey") {
      const dimSource = config?.dimensions[0] ?? dim;
      const dimTarget = config?.dimensions[1];
      const valKey = measureFields[0]?.field ?? measureKey;
      if (dimSource && data.length > 0) {
        return <EChartsSankey data={data} sourceField={dimSource} targetField={dimTarget} valueField={valKey} colors={COLORS} onEvents={onEvents} />;
      }
      return (
        <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
          桑基图需要至少 1 个维度 + 1 个度量
        </div>
      );
    }

    // 散点图
    if (chartType === "scatter") {
      const xKey = measureFields[0]?.field ?? dim;
      const yKey = measureFields[1]?.field ?? measureFields[0]?.field ?? measureKey;
      return <EChartsScatter data={data} xField={xKey} yField={yKey} colors={COLORS} onEvents={onEvents} />;
    }

    // KPI 卡片：大数字展示
    if (chartType === "kpi_card") {
      const kpiVal = data[0]?.[measureFields[0]?.field ?? measureKey];
      return (
        <div className="flex flex-col items-center justify-center h-full">
          <span className="text-[11px] text-muted-foreground mb-1">
            {measureFields[0]?.label ?? measureKey}
          </span>
          <span className="text-4xl font-bold" style={{ color: COLORS[0] }}>
            {kpiVal != null ? Number(kpiVal).toLocaleString() : "-"}
          </span>
        </div>
      );
    }

    // 柱状图、折线图、面积图（含 grouped_bar、stacked_bar 变体）
    const isStacked = chartType === "stacked_bar";
    const commonProps = { data, xField: dim };

    if (measureFields.length > 0) {
      switch (chartType) {
        case "bar":
        case "grouped_bar":
        case "stacked_bar":
          return (
            <EChartsBar
              {...commonProps}
              measureFields={measureFields}
              colors={COLORS}
              stacked={isStacked}
              onEvents={onEvents}
            />
          );
        case "horizontal_bar":
          // 水平条形：x/y 轴对调，类目在 Y 轴；多度量时也支持双 Y 轴
          return (
            <EChartsHorizontalBar
              {...commonProps}
              measureFields={measureFields}
              colors={COLORS}
              stacked={isStacked}
              onEvents={onEvents}
            />
          );
        case "line":
          return (
            <EChartsLine
              {...commonProps}
              measureFields={measureFields}
              colors={COLORS}
              onEvents={onEvents}
            />
          );
        case "area":
          return (
            <EChartsArea
              {...commonProps}
              measureFields={measureFields}
              colors={COLORS}
              onEvents={onEvents}
            />
          );
      }
    }

    // 兜底：单度量模式
    switch (chartType) {
      case "bar":
      case "grouped_bar":
      case "stacked_bar":
        return <EChartsBar data={data} xField={dim} yField={measureKey} color={COLORS[0]} onEvents={onEvents} />;
      case "horizontal_bar":
        return <EChartsHorizontalBar data={data} xField={dim} yField={measureKey} color={COLORS[0]} onEvents={onEvents} />;
      case "line":
        return <EChartsLine data={data} xField={dim} yField={measureKey} color={COLORS[0]} onEvents={onEvents} />;
      case "area":
        return <EChartsArea data={data} xField={dim} yField={measureKey} color={COLORS[0]} onEvents={onEvents} />;
      default:
        break;
    }
  }

  // --- Recharts 渲染（支持多度量 + 图例）---
  const showLegend = measures.length > 1;
  const isStacked = chartType === "stacked_bar";

  // 双 Y 轴：计算每个度量用左轴还是右轴（跟 ECharts 逻辑一致：最大值在左，其余在右）
  const useDualAxis = measures.length >= 2;
  const axisAssignments: number[] = (() => {
    if (!useDualAxis) return [];
    const maxes = measureFields.map((f) => {
      let max = 0;
      for (const row of data) {
        const v = Number(row[f.field]) || 0;
        if (v > max) max = v;
      }
      return max;
    });
    const result: number[] = new Array(maxes.length).fill(0);
    const globalMax = Math.max(...maxes);
    maxes.forEach((m, i) => {
      if (m > 0 && m < globalMax) result[i] = 1;
    });
    if (!result.includes(0) && result.length > 0) result[0] = 0;
    return result;
  })();

  switch (chartType) {
    case "bar":
    case "grouped_bar":
    case "stacked_bar":
      return (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: useDualAxis ? 8 : 8, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" vertical={false} />
            <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={false} tickLine={false} width={60} tickFormatter={formatYAxis} />
            {useDualAxis && (
              <YAxis
                yAxisId="right"
                orientation="right"
                tick={{ fontSize: 11, fill: "#8B97A8" }}
                axisLine={false}
                tickLine={false}
                width={60}
                tickFormatter={formatYAxis}
              />
            )}
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E2E8F0",
                borderRadius: 8,
                fontSize: 12,
                color: "#1A2332",
              }}
              formatter={(value, name) => [Number(value ?? 0).toLocaleString(), String(name)]}
            />
            {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
            {measureFields.length > 0 ? (
              measureFields.map((m, i) => (
                <Bar
                  key={m.field}
                  dataKey={m.field}
                  name={m.label}
                  fill={COLORS[i % COLORS.length]}
                  radius={[4, 4, 0, 0]}
                  stackId={isStacked ? "stack" : undefined}
                  yAxisId={useDualAxis ? (axisAssignments[i] === 0 ? "left" : "right") : "left"}
                />
              ))
            ) : (
              <Bar dataKey={measureKey} fill={COLORS[0]} radius={[4, 4, 0, 0]}>
                {data.length > 1 ? (
                  data.map((_, idx) => (
                    <Cell key={idx} fill={COLORS[idx % COLORS.length]} />
                  ))
                ) : null}
              </Bar>
            )}
          </BarChart>
        </ResponsiveContainer>
      );

    case "line":
      if (useDualAxis) {
        return (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 8, right: 60, left: 0, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" vertical={false} />
              <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
              <YAxis
                yAxisId="left"
                tick={{ fontSize: 11, fill: "#8B97A8" }}
                axisLine={false}
                tickLine={false}
                width={60}
                tickFormatter={formatYAxis}
              />
              <YAxis
                yAxisId="right"
                orientation="right"
                tick={{ fontSize: 11, fill: "#8B97A8" }}
                axisLine={false}
                tickLine={false}
                width={60}
                tickFormatter={formatYAxis}
              />
              <Tooltip
                contentStyle={{
                  background: "#FFFFFF",
                  border: "1px solid #E2E8F0",
                  borderRadius: 8,
                  fontSize: 12,
                  color: "#1A2332",
                }}
                formatter={(value, name) => [Number(value ?? 0).toLocaleString(), String(name)]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {measureFields.map((m, i) => (
                <Line
                  key={m.field}
                  type="monotone"
                  dataKey={m.field}
                  name={m.label}
                  stroke={COLORS[i % COLORS.length]}
                  strokeWidth={2.5}
                  dot={{ r: 3, fill: "#FFFFFF", stroke: COLORS[i % COLORS.length], strokeWidth: 2 }}
                  activeDot={{ r: 5 }}
                  yAxisId={axisAssignments[i] === 0 ? "left" : "right"}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        );
      }
      return (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" vertical={false} />
            <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={false} tickLine={false} width={60} tickFormatter={formatYAxis} />
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E2E8F0",
                borderRadius: 8,
                fontSize: 12,
                color: "#1A2332",
              }}
              formatter={(value, name) => [Number(value ?? 0).toLocaleString(), String(name)]}
            />
            {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
            {measureFields.length > 0 ? (
              measureFields.map((m, i) => (
                <Line
                  key={m.field}
                  type="monotone"
                  dataKey={m.field}
                  name={m.label}
                  stroke={COLORS[i % COLORS.length]}
                  strokeWidth={2.5}
                  dot={{ r: 3, fill: "#FFFFFF", stroke: COLORS[i % COLORS.length], strokeWidth: 2 }}
                  activeDot={{ r: 5 }}
                />
              ))
            ) : (
              <Line
                type="monotone"
                dataKey={measureKey}
                stroke={COLORS[0]}
                strokeWidth={2.5}
                dot={{ r: 3, fill: "#FFFFFF", stroke: COLORS[0], strokeWidth: 2 }}
                activeDot={{ r: 5 }}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      );

    case "area":
      if (useDualAxis) {
        return (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 8, right: 60, left: 0, bottom: 8 }}>
              <defs>
                {COLORS.map((c, i) => (
                  <linearGradient key={i} id={`area-r-${c}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={c} stopOpacity={0.3} />
                    <stop offset="100%" stopColor={c} stopOpacity={0.02} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" vertical={false} />
              <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
              <YAxis
                yAxisId="left"
                tick={{ fontSize: 11, fill: "#8B97A8" }}
                axisLine={false}
                tickLine={false}
                width={60}
                tickFormatter={formatYAxis}
              />
              <YAxis
                yAxisId="right"
                orientation="right"
                tick={{ fontSize: 11, fill: "#8B97A8" }}
                axisLine={false}
                tickLine={false}
                width={60}
                tickFormatter={formatYAxis}
              />
              <Tooltip
                contentStyle={{
                  background: "#FFFFFF",
                  border: "1px solid #E2E8F0",
                  borderRadius: 8,
                  fontSize: 12,
                  color: "#1A2332",
                }}
                formatter={(value, name) => [Number(value ?? 0).toLocaleString(), String(name)]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {measureFields.map((m, i) => (
                <Area
                  key={m.field}
                  type="monotone"
                  dataKey={m.field}
                  name={m.label}
                  stroke={COLORS[i % COLORS.length]}
                  strokeWidth={2}
                  fill={`url(#area-r-${COLORS[i % COLORS.length]})`}
                  yAxisId={axisAssignments[i] === 0 ? "left" : "right"}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        );
      }
      return (
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
            <defs>
              {COLORS.map((c, i) => (
                <linearGradient key={i} id={`area-${c}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={c} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={c} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" vertical={false} />
            <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={false} tickLine={false} width={60} tickFormatter={formatYAxis} />
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E2E8F0",
                borderRadius: 8,
                fontSize: 12,
                color: "#1A2332",
              }}
              formatter={(value, name) => [Number(value ?? 0).toLocaleString(), String(name)]}
            />
            {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
            {measureFields.length > 0 ? (
              measureFields.map((m, i) => (
                <Area
                  key={m.field}
                  type="monotone"
                  dataKey={m.field}
                  name={m.label}
                  stroke={COLORS[i % COLORS.length]}
                  strokeWidth={2}
                  fill={`url(#area-${COLORS[i % COLORS.length]})`}
                />
              ))
            ) : (
              <Area
                type="monotone"
                dataKey={measureKey}
                stroke={COLORS[0]}
                strokeWidth={2}
                fill={`url(#area-${COLORS[0]})`}
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      );

    case "donut":
    case "pie":
      return (
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              dataKey={measureKey}
              nameKey={dim}
              cx="50%"
              cy="50%"
              outerRadius="70%"
              innerRadius={chartType === "donut" ? "45%" : "0%"}
              paddingAngle={2}
            >
              {data.map((_, idx) => (
                <Cell key={idx} fill={COLORS[idx % COLORS.length]} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E2E8F0",
                borderRadius: 8,
                fontSize: 12,
              }}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
          </PieChart>
        </ResponsiveContainer>
      );

    case "scatter":
      return (
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#F0F3F7" />
            <XAxis dataKey={dim} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={{ stroke: "#E2E8F0" }} tickLine={false} />
            <YAxis dataKey={measureKey} tick={{ fontSize: 11, fill: "#8B97A8" }} axisLine={false} tickLine={false} width={60} tickFormatter={formatYAxis} />
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E2E8F0",
                borderRadius: 8,
                fontSize: 12,
                color: "#1A2332",
              }}
              formatter={(value) => [Number(value ?? 0).toLocaleString(), measureKey]}
            />
            <Scatter data={data} fill={COLORS[0]} />
          </ScatterChart>
        </ResponsiveContainer>
      );

    case "kpi_card":
      return (
        <div className="flex flex-col items-center justify-center h-full">
          <span className="text-[11px] text-muted-foreground mb-1">
            {measureFields[0]?.label ?? measureKey}
          </span>
          <span className="text-4xl font-bold" style={{ color: COLORS[0] }}>
            {data[0]?.[measureFields[0]?.field ?? measureKey] != null
              ? Number(data[0]?.[measureFields[0]?.field ?? measureKey]).toLocaleString()
              : "-"}
          </span>
        </div>
      );

    default:
      return (
        <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
          不支持的图表类型：{chartTypeCn(chartType)}
        </div>
      );
  }
}
