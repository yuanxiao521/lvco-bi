import { memo } from "react";
import { Loader2, CheckCircle2, XCircle, ChevronDown, Sparkles } from "lucide-react";
import { useState } from "react";

// 工具调用项：画布智能体实时执行的一个工具
export interface FeedTool {
  name: string;
  args?: Record<string, unknown>;
  result?: string;
  status: "run" | "ok" | "err";
}

// 时间线步骤：一组相关的工具调用 + 进度状态
export interface FeedStep {
  id: string;
  title: string;
  status: "wait" | "run" | "done" | "failed";
  tools: FeedTool[];
}

interface ActivityFeedProps {
  steps: FeedStep[];
}

/** 工具名 → 中文展示名映射 */
const TOOL_LABEL: Record<string, string> = {
  list_datasources: "浏览数据源",
  query_datasource: "查询数据",
  query_engine: "结构化查询",
  insight: "自动洞察",
  data_quality: "数据质量",
  clean_suggest: "清洗建议",
  stats_analyzer: "统计分析",
  render_chart: "生成图表",
  validate_chart: "校验图表",
  recommend_charts: "推荐图表",
  polish_text: "润色文本",
  add_chart_block: "新增图表块",
  add_text_block: "写入文本块",
  update_chart_block: "修改图表块",
  remove_block: "删除块",
  arrange_layout: "自动布局",
};

/** 汇总工具结果：取首行 error 或 ok，供 chip 状态展示 */
function summarizeResult(result?: string): { status: "ok" | "err"; hint: string } {
  if (!result) return { status: "ok", hint: "" };
  try {
    const r = JSON.parse(result);
    if (r?.error) return { status: "err", hint: String(r.error).slice(0, 160) };
    const rows = Array.isArray(r?.rows) ? r.rows.length : "";
    const cnt = r?.row_count ?? "";
    return { status: "ok", hint: rows !== "" ? `${r?.columns?.length ?? 0} 列 · ${rows || cnt} 行` : "完成" };
  } catch {
    return { status: "ok", hint: "" };
  }
}

/** 单个工具的折叠卡片 */
function ToolRow({ tool }: { tool: FeedTool }) {
  const [open, setOpen] = useState(false);
  const summary = tool.status !== "run" ? summarizeResult(tool.result) : null;
  return (
    <div className="border border-border/60 rounded-[8px] bg-background/60 overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-2 py-1.5 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="flex-shrink-0">
          {tool.status === "run" ? (
            <Loader2 className="w-3 h-3 animate-spin text-ai" />
          ) : summary?.status === "err" ? (
            <XCircle className="w-3 h-3 text-error" />
          ) : (
            <CheckCircle2 className="w-3 h-3 text-success" />
          )}
        </span>
        <span className="text-[12px] font-medium text-foreground flex-1">
          {TOOL_LABEL[tool.name] ?? tool.name}
        </span>
        <span className="text-[10px] text-muted-foreground font-mono">{tool.status === "run" ? "执行中" : summary?.hint}</span>
        <ChevronDown className={`w-3 h-3 text-muted-foreground/60 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <pre className="px-2 pb-2 text-[10.5px] text-muted-foreground font-mono whitespace-pre-wrap overflow-auto max-h-36">
          {JSON.stringify({ name: tool.name, args: tool.args, result: summary?.status === "err" ? summary.hint : undefined }, null, 2).slice(0, 900)}
        </pre>
      )}
    </div>
  );
}

const STATUS_META: Record<FeedStep["status"], { dot: string; label: string }> = {
  wait: { dot: "border-border text-muted-foreground", label: "待执行" },
  run: { dot: "bg-ai text-white border-ai", label: "处理中" },
  done: { dot: "text-success border-success", label: "完成" },
  failed: { dot: "text-error border-error", label: "失败" },
};

/**
 * 画布智能体工作台：步骤时间线 + 工具调用明细（折叠可看参数 / 自纠错结果）。
 * 对应后端 SSE 的 step / tool_call / tool_result / plan 事件。
 */
function ActivityFeed({ steps }: ActivityFeedProps) {
  if (!steps.length) return null;
  return (
    <div className="px-3 py-2 bg-ai-light/40 border-y border-border-light">
      <div className="flex items-center gap-1.5 mb-2 text-[11px] font-semibold text-ai">
        <Sparkles className="w-3 h-3" />
        Agent 工作台
        <span className="text-[10px] font-normal text-muted-foreground ml-auto">
          {steps.filter((s) => s.status === "done").length}/{steps.length} 完成
        </span>
      </div>
      <ol className="space-y-2">
        {steps.map((step) => {
          const meta = STATUS_META[step.status];
          return (
            <li key={step.id}>
              <div className="flex items-center gap-2">
                <span className={`w-4 h-4 shrink-0 rounded-full flex items-center justify-center text-[9px] border ${meta.dot}`}>
                  {step.status === "done" ? "✓" : step.status === "failed" ? "!" : step.status === "run" ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : ""}
                </span>
                <span className="text-[12px] text-foreground flex-1">{step.title}</span>
                <span className="text-[10px] text-muted-foreground">{meta.label}</span>
              </div>
              {step.tools.length > 0 && (
                <div className="mt-1.5 ml-6 space-y-1">
                  {step.tools.map((t, i) => <ToolRow key={i} tool={t} />)}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export default memo(ActivityFeed);