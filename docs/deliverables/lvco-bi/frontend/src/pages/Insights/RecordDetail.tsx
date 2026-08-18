import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Lightbulb,
  ArrowLeft,
  FileText,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
  Brain,
  AlertTriangle,
  Database,
  Calendar,
} from "lucide-react";
import {
  getInsightRule,
  getInsightRecord,
  type InsightRecord,
  type InsightRule,
} from "../../api/insights";

export default function RecordDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [record, setRecord] = useState<InsightRecord | null>(null);
  const [rule, setRule] = useState<InsightRule | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    void load();
  }, [id]);

  async function load() {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const r = await getInsightRecord(id);
      setRecord(r);
      // 顺手把规则名也拿一下
      try {
        const rl = await getInsightRule(r.ruleId);
        setRule(rl);
      } catch {
        /* ignore */
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载记录失败");
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        加载中...
      </div>
    );
  }

  if (error || !record) {
    return (
      <div className="flex-1 p-6">
        <div className="flex items-center gap-2 text-danger text-[13px] mb-4">
          <AlertCircle className="w-4 h-4" />
          {error || "记录不存在"}
        </div>
        <button
          onClick={() => navigate(-1)}
          className="px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted"
        >
          返回
        </button>
      </div>
    );
  }

  const anomalies = (record.detectedAnomalies ?? []) as Array<{
    type?: string;
    field?: string;
    value?: number | string;
    expected?: number | string;
    deviation?: number;
    message?: string;
  }>;
  const status = record.status;

  return (
    <div className="flex-1 px-6 py-6 overflow-auto max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-5">
        <button
          onClick={() => navigate(-1)}
          className="p-1.5 rounded-md hover:bg-muted text-muted-foreground"
          title="返回"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <Lightbulb className="w-5 h-5 text-primary" />
        <div className="flex-1">
          <h1 className="text-[18px] font-semibold">洞察执行记录</h1>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            规则: {record.ruleName || rule?.name || "—"}
          </p>
        </div>
        <StatusBadge status={status} />
      </div>

      {/* 元信息卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-5">
        <MetaCard
          icon={Calendar}
          label="执行时间"
          value={new Date(record.runAt).toLocaleString("zh-CN")}
        />
        <MetaCard
          icon={Database}
          label="数据周期"
          value={`${record.periodStart?.slice(0, 10) ?? "—"} ~ ${record.periodEnd?.slice(0, 10) ?? "—"}`}
        />
        <MetaCard
          icon={AlertTriangle}
          label="异常数量"
          value={`${anomalies.length} 项`}
        />
      </div>

      {/* 错误信息 */}
      {record.errorMessage && (
        <div className="mb-5 flex items-start gap-2 px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">
          <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <div>
            <div className="font-medium">执行失败</div>
            <div className="text-[12px] mt-1 opacity-90">{record.errorMessage}</div>
          </div>
        </div>
      )}

      {/* AI 解读 */}
      {record.aiNarrative && (
        <div className="bg-gradient-to-br from-primary/5 to-ai/5 border border-primary/20 rounded-lg p-5 mb-5">
          <div className="flex items-center gap-2 mb-3">
            <Brain className="w-4 h-4 text-primary" />
            <h3 className="text-[13px] font-semibold">AI 智能解读</h3>
          </div>
          <div className="text-[13px] leading-relaxed text-foreground whitespace-pre-wrap">
            {record.aiNarrative}
          </div>
        </div>
      )}

      {/* 异常列表 */}
      <div className="bg-white border border-border-light rounded-lg p-5 mb-5">
        <h3 className="text-[13px] font-semibold mb-3 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-warning" />
          检测到的异常 ({anomalies.length})
        </h3>
        {anomalies.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground text-[12px]">
            {status === "success" ? "本次执行未发现异常 ✓" : "无异常数据"}
          </div>
        ) : (
          <div className="space-y-2">
            {anomalies.map((a, idx) => (
              <div
                key={idx}
                className="flex items-start gap-3 p-3 border border-border-light rounded-md hover:border-warning/50 transition-colors"
              >
                <div className="w-7 h-7 rounded-full bg-warning-light flex items-center justify-center flex-shrink-0">
                  <AlertTriangle className="w-3.5 h-3.5 text-warning" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-[12px] font-medium">
                      {a.type || "anomaly"}
                    </span>
                    {a.field && (
                      <span className="text-[11px] text-muted-foreground">
                        字段: {a.field}
                      </span>
                    )}
                  </div>
                  <div className="text-[12px] text-foreground">
                    {a.message ||
                      `当前值 ${a.value ?? "—"}，期望 ${a.expected ?? "—"}` +
                        (typeof a.deviation === "number"
                          ? `，偏离 ${a.deviation.toFixed(2)}`
                          : "")}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 底部：跳转 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button
            onClick={() => navigate(`/insights/rules/${record.ruleId}`)}
            className="px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted flex items-center gap-1"
          >
            <FileText className="w-3.5 h-3.5" />
            查看洞察详情
          </button>
          {record.reportId && (
            <button
              onClick={() => navigate(`/report-center/${record.reportId}`)}
              className="px-3 py-1.5 text-[12px] bg-primary text-white rounded-md hover:bg-primary-hover flex items-center gap-1"
            >
              <Brain className="w-3.5 h-3.5" />
              查看日报
            </button>
          )}
        </div>
        <div className="text-[11px] text-muted-foreground">
          Token 消耗: {(record.llmTokensInput ?? 0) + (record.llmTokensOutput ?? 0)}
        </div>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (status === "success") {
    return (
      <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-50 text-emerald-600 flex items-center gap-1">
        <CheckCircle2 className="w-3 h-3" />
        成功
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="text-[10px] px-2 py-0.5 rounded bg-red-50 text-red-600 flex items-center gap-1">
        <XCircle className="w-3 h-3" />
        失败
      </span>
    );
  }
  return (
    <span className="text-[10px] px-2 py-0.5 rounded bg-muted text-muted-foreground flex items-center gap-1">
      <Loader2 className="w-3 h-3" />
      {status}
    </span>
  );
}

function MetaCard({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Calendar;
  label: string;
  value: string;
}) {
  return (
    <div className="bg-white border border-border-light rounded-lg p-4">
      <div className="flex items-center gap-2 text-muted-foreground text-[11px] mb-1">
        <Icon className="w-3.5 h-3.5" />
        {label}
      </div>
      <div className="text-[13px] font-medium truncate">{value}</div>
    </div>
  );
}
