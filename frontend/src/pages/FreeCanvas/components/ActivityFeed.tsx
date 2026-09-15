import { memo, useEffect, useState } from "react";
import { Loader2, CheckCircle2, XCircle, ChevronDown, ListTree } from "lucide-react";

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
  /** 当前执行中高亮（progress status=start 时置 true） */
  emphasis?: boolean;
  /** 是否展开子工具列表 */
  expanded?: boolean;
}

/** LeadAgent 整轮元信息：意图 + 决策 + 降级标记（progress 之外的增量事件） */
export interface AgentMeta {
  /** intent 原始 key：analysis / data_qa / chat / canvas_edit / followup */
  intent?: string;
  intentConfidence?: number;
  /** decision action 原始 key：call_analysis / answer / canvas_op / ask_user */
  decision?: string;
  decisionTool?: string;
  decisionReason?: string;
  degraded?: boolean;
}

interface ActivityFeedProps {
  steps: FeedStep[];
  /** LeadAgent 增量元信息（可选）；不传时保持旧行为 */
  meta?: AgentMeta;
}

/** progress 事件 status → FeedStep.status 映射（供各页面复用） */
export function mapProgressStatus(status: string): FeedStep["status"] {
  if (status === "start") return "run";
  if (status === "ok") return "done";
  if (status === "error" || status === "fail") return "failed";
  // skip 也视为完成（执行方主动跳过）
  if (status === "skip") return "done";
  return "wait";
}

/** 意图 → 中文展示名 */
const INTENT_LABEL: Record<string, string> = {
  analysis: "数据分析",
  data_qa: "数据问答",
  chat: "闲聊",
  canvas_edit: "画布操作",
  followup: "追问",
};

/** 决策动作 → 中文展示名 */
const ACTION_LABEL: Record<string, string> = {
  call_analysis: "分析引擎",
  answer: "直接回答",
  canvas_op: "画布操作",
  ask_user: "询问补充",
};

/** 工具名 → 中文展示名映射（对齐后端全部工具：agent_tools 12 + canvas_tools 5 + run_analysis） */
const TOOL_LABEL: Record<string, string> = {
  run_analysis: "分析执行",
  list_datasources: "浏览数据源",
  list_fields: "查看字段",
  query_sql: "SQL 查询",
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
    <div className="border border-border/60 rounded-[6px] bg-background/60 overflow-hidden">
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
        <span className="text-[12px] font-medium text-foreground flex-1 truncate">
          {TOOL_LABEL[tool.name] ?? tool.name}
        </span>
        <span className="text-[10px] text-muted-foreground font-mono shrink-0">{tool.status === "run" ? "执行中" : summary?.hint}</span>
        <ChevronDown className={`w-3 h-3 text-muted-foreground/60 transition-transform shrink-0 ${open ? "rotate-180" : ""}`} />
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
 * 单个步骤行：可展开/折叠，展示其下属工具调用列表。
 *
 * 层级结构：
 *   ┌─ 步骤 1：分析执行 ────────────────── 完成 ─┐
 *   │   ├─ 浏览数据源  · 完成                      │
 *   │   ├─ SQL 查询    · 完成                      │
 *   │   └─ 生成图表    · 完成                      │
 *   └─────────────────────────────────────────────┘
 */
function StepRow({ step, idx }: { step: FeedStep; idx: number }) {
  const [expanded, setExpanded] = useState(step.expanded ?? false);
  const metaInfo = STATUS_META[step.status];
  const hasTools = step.tools.length > 0;

  // 同步外部 expanded 状态
  useEffect(() => {
    setExpanded(step.expanded ?? false);
  }, [step.expanded]);

  return (
    <li key={step.id}>
      {/* 步骤标题行 */}
      <div className="flex items-center gap-2">
        {/* 展开箭头（有子工具时显示） */}
        {hasTools ? (
          <button
            className="flex-shrink-0 p-0.5 rounded hover:bg-muted/60 transition-colors"
            onClick={() => setExpanded((v) => !v)}
          >
            <ChevronDown className={`w-3 h-3 text-muted-foreground transition-transform ${expanded ? "rotate-180" : ""}`} />
          </button>
        ) : (
          <span className="w-4 flex-shrink-0" />
        )}
        {/* 状态圆点 */}
        <span className={`w-4 h-4 shrink-0 rounded-full flex items-center justify-center border ${metaInfo.dot}`}>
          {step.status === "done" ? (
            <CheckCircle2 className="w-3 h-3" />
          ) : step.status === "failed" ? (
            <XCircle className="w-3 h-3" />
          ) : step.status === "run" ? (
            <Loader2 className="w-2.5 h-2.5 animate-spin" />
          ) : null}
        </span>
        {/* 步骤序号 + 标题 */}
        <span className="text-[10px] font-mono text-muted-foreground/70 shrink-0 w-4">{idx + 1}</span>
        <span className={`text-[12px] flex-1 truncate ${step.emphasis ? "text-ai font-medium" : "text-foreground"}`}>
          {step.title}
        </span>
        {/* 工具计数 + 状态标签 */}
        {hasTools && (
          <span className="text-[10px] text-muted-foreground/70 shrink-0">
            {step.tools.filter((t) => t.status === "ok").length}/{step.tools.length}
          </span>
        )}
        <span className="text-[10px] text-muted-foreground shrink-0">{metaInfo.label}</span>
      </div>
      {/* 子工具列表（展开时显示） */}
      {expanded && hasTools && (
        <div className="mt-1.5 ml-6 space-y-1">
          {step.tools.map((t, i) => <ToolRow key={i} tool={t} />)}
        </div>
      )}
    </li>
  );
}

/**
 * Agent 执行记录：气泡内收敛卡片。
 *
 * 交互：默认折叠成一条"概要栏"（当前执行进度 / 完成状态），点击展开完整时间线。
 * 视觉：独立卡片（圆角 + 边框 + 卡片底色），内部用分隔线区分「元信息区」与「步骤时间线」，
 * 与 AI 回复正文彻底分区——对话是对话，执行过程是执行过程。
 *
 * 层级结构：步骤 → 工具调用，每层可独立展开/折叠。
 */
function ActivityFeed({ steps, meta }: ActivityFeedProps) {
  const [open, setOpen] = useState(false);
  if (!steps.length && !meta) return null;

  const doneCount = steps.filter((s) => s.status === "done").length;
  const running = steps.some((s) => s.status === "run" || s.tools.some((t) => t.status === "run"));
  const failed = steps.some((s) => s.status === "failed");
  const hasMetaInfo = Boolean(meta?.intent || meta?.decision || meta?.degraded);

  // 执行中自动展开（让用户看到实时进度）；非执行中保持用户手动状态
  useEffect(() => {
    if (running) setOpen(true);
  });

  // 概要栏：当前正在执行的步骤（取第一个 run 步骤，无则取最后一个完成的）
  const activeStep =
    steps.find((s) => s.status === "run") ||
    (steps.length > 0 ? steps[steps.length - 1] : undefined);

  const summaryText = running
    ? `${activeStep?.title || "执行中"}`
    : failed
      ? "执行完成，部分步骤失败"
      : doneCount > 0
        ? `完成 ${doneCount}/${steps.length} 步`
        : "Agent 已空闲";

  return (
    <div className="mt-2 rounded-[8px] border border-border-light overflow-hidden bg-card">
      {/* 概要栏：默认折叠展示 */}
      <button
        className="w-full flex items-center gap-2 px-2.5 py-2 text-left hover:bg-muted/40 transition-colors"
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center ${running ? "bg-ai-light text-ai" : failed ? "bg-error/10 text-error" : "bg-success/10 text-success"}`}>
          {running ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : failed ? (
            <XCircle className="w-3 h-3" />
          ) : (
            <ListTree className="w-3 h-3" />
          )}
        </span>
        <span className="flex-1 min-w-0">
          <span className="block text-[11px] font-semibold text-foreground leading-tight">
            Agent 执行记录
            {running && <span className="ml-1.5 text-ai text-[10px] font-normal">执行中</span>}
          </span>
          <span className={`block text-[10px] leading-tight truncate ${running ? "text-muted-foreground" : "text-muted-foreground/80"}`}>
            {summaryText}
          </span>
        </span>
        <ChevronDown className={`w-3.5 h-3.5 text-muted-foreground/60 transition-transform shrink-0 ${open ? "rotate-180" : ""}`} />
      </button>

      {/* 展开区：元信息 + 步骤时间线（分隔线分区） */}
      {open && (
        <div className="px-2.5 pb-2.5 pt-0.5">
          {hasMetaInfo && (
            <div className="flex flex-wrap items-center gap-1.5 py-2 border-b border-border-light/60">
              {meta?.intent && (
                <span title={`意图识别 · confidence=${meta.intentConfidence ?? "-"}`}
                      className="rounded-full border border-border/60 text-[10px] px-2 py-0.5 text-ai bg-background/40">
                  意图：{INTENT_LABEL[meta.intent] ?? meta.intent}
                </span>
              )}
              {meta?.decision && (
                <span title={meta.decisionReason ? `决策依据：${meta.decisionReason}` : undefined}
                      className="rounded-full border border-border/60 text-[10px] px-2 py-0.5 text-muted-foreground bg-background/40">
                  决策：{ACTION_LABEL[meta.decision] ?? meta.decision}
                  {meta.decisionTool ? <span className="font-mono"> · {meta.decisionTool}</span> : null}
                </span>
              )}
              {meta?.degraded && (
                <span className="rounded-full border border-amber-600/50 text-[10px] px-2 py-0.5 text-amber-600 bg-amber-500/10">
                  ● 降级执行
                </span>
              )}
            </div>
          )}
          {steps.length > 0 && (
            <ol className="py-2 space-y-2">
              {steps.map((step, i) => (
                <StepRow key={step.id} step={step} idx={i} />
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

export default memo(ActivityFeed);
