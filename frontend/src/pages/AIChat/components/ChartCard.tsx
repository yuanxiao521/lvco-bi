import { useRef, useEffect, useState } from "react";
import * as echarts from "echarts";
import { Download } from "lucide-react";
import { CHART_TYPE_LABELS } from "../../../components/charts";
import { formatYAxisNum } from "../../../components/charts/echarts/echartsUtils";

interface Props {
  chartType: string;
  option: Record<string, unknown>;
}

/** 后端在 axisLabel.formatter 字段放置的占位符，前端在 setOption 前替换为真实函数 */
const FMT_Y_PLACEHOLDER = "__lvco_fmt_y__";

/** 递归遍历 option，将后端占位符替换为真正的 ECharts 函数引用 */
function hydrateOptionFormatters(node: unknown): unknown {
  if (Array.isArray(node)) {
    return node.map((item) => hydrateOptionFormatters(item));
  }
  if (node && typeof node === "object") {
    const obj = node as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      if (k === "formatter" && typeof v === "string" && v === FMT_Y_PLACEHOLDER) {
        out[k] = (val: number) => formatYAxisNum(val);
      } else {
        out[k] = hydrateOptionFormatters(v);
      }
    }
    return out;
  }
  return node;
}

export default function ChartCard({ chartType, option }: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);

  useEffect(() => {
    if (!chartRef.current) return;
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
    }
    // 防御：option 不是合法 echarts 对象时跳过渲染，避免抛出导致整页白屏
    try {
      if (!option || typeof option !== "object" || !("series" in option)) {
        setRenderError("图表配置缺失或格式错误，已跳过渲染");
        return;
      }
      const series = (option as Record<string, unknown>).series;
      if (series != null && !Array.isArray(series) && typeof series !== "object") {
        setRenderError("图表 series 字段类型不合法");
        return;
      }
      instanceRef.current.setOption(hydrateOptionFormatters(option) as echarts.EChartsOption, true);
      setRenderError(null);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[ChartCard] setOption failed:", e);
      setRenderError(e instanceof Error ? e.message : "图表渲染失败");
    }

    const handleResize = () => instanceRef.current?.resize();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
    };
  }, [option]);

  // P3: 组件卸载时才销毁 ECharts 实例
  useEffect(() => {
    return () => {
      instanceRef.current?.dispose();
      instanceRef.current = null;
    };
  }, []);

  const handleDownload = () => {
    if (!instanceRef.current) return;
    const url = instanceRef.current.getDataURL({ type: "png", pixelRatio: 2 });
    const a = document.createElement("a");
    a.href = url;
    a.download = `chart_${Date.now()}.png`;
    a.click();
  };

  return (
    <div className="border border-border-light rounded-lg overflow-hidden bg-white mt-2">
      <div className="flex items-center justify-between px-3 py-2 bg-gray-50 border-b border-border-light">
        <span className="text-xs font-medium text-muted-foreground">
          {CHART_TYPE_LABELS[chartType as keyof typeof CHART_TYPE_LABELS] ?? chartType}
        </span>
        <button
          onClick={handleDownload}
          className="p-1 hover:bg-gray-200 rounded text-muted-foreground transition-colors"
          title="下载图表"
        >
          <Download size={14} />
        </button>
      </div>
      <div ref={chartRef} className="w-full h-[280px] relative">
        {renderError && (
          <div className="absolute inset-0 flex items-center justify-center text-[12px] text-muted-foreground bg-muted/40">
            {renderError}
          </div>
        )}
      </div>
    </div>
  );
}
