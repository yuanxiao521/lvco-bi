import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Lightbulb,
  ArrowLeft,
  Play,
  Pause,
  Pencil,
  Trash2,
  Database,
  Calendar,
  Activity,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
  FileText,
} from "lucide-react";
import {
  getInsightRule,
  runInsightRuleNow,
  deleteInsightRule,
  updateInsightRule,
  listInsightRecords,
  type InsightRule,
  type InsightRecord,
} from "../../api/insights";

export default function RuleDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [rule, setRule] = useState<InsightRule | null>(null);
  const [records, setRecords] = useState<InsightRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    void load();
  }, [id]);

  async function load() {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const r = await getInsightRule(id);
      setRule(r);
      const recs = await listInsightRecords({ ruleId: id, pageSize: 20 });
      setRecords(recs.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载洞察失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleRun() {
    if (!rule) return;
    setRunning(true);
    setRunResult(null);
    try {
      const result = await runInsightRuleNow(rule.id);
      setRunResult(`执行完成，状态: ${result.status}`);
      await load();
    } catch (e) {
      setRunResult(e instanceof Error ? e.message : "执行失败");
    } finally {
      setRunning(false);
    }
  }

  async function handleToggleEnabled() {
    if (!rule) return;
    try {
      const updated = await updateInsightRule(rule.id, { enabled: !rule.enabled });
      setRule(updated);
    } catch (e) {
      alert(e instanceof Error ? e.message : "更新失败");
    }
  }

  async function handleDelete() {
    if (!rule) return;
    if (!window.confirm(`确定删除洞察"${rule.name}"？此操作不可恢复。`)) return;
    try {
      await deleteInsightRule(rule.id);
      navigate("/insights");
    } catch (e) {
      alert(e instanceof Error ? e.message : "删除失败");
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

  if (error || !rule) {
    return (
      <div className="flex-1 p-6">
        <div className="flex items-center gap-2 text-danger text-[13px]">
          <AlertCircle className="w-4 h-4" />
          {error || "洞察不存在"}
        </div>
        <button
          onClick={() => navigate("/insights")}
          className="mt-4 px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted"
        >
          返回列表
        </button>
      </div>
    );
  }

  const qc = rule.queryConfig;
  const lastStatus = rule.lastRunStatus;

  return (
    <div className="flex-1 px-6 py-6 overflow-auto max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-5">
        <button
          onClick={() => navigate("/insights")}
          className="p-1.5 rounded-md hover:bg-muted text-muted-foreground"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <Lightbulb className="w-5 h-5 text-primary" />
        <div className="flex-1">
          <h1 className="text-[18px] font-semibold">{rule.name}</h1>
          {rule.description && (
            <p className="text-[12px] text-muted-foreground mt-0.5">{rule.description}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`text-[10px] px-2 py-0.5 rounded ${
              rule.enabled ? "bg-emerald-50 text-emerald-600" : "bg-muted text-muted-foreground"
            }`}
          >
            {rule.enabled ? "已启用" : "已暂停"}
          </span>
        </div>
      </div>

      {/* Action Bar */}
      <div className="flex items-center gap-2 mb-5">
        <button
          onClick={handleRun}
          disabled={running}
          className="px-3 py-1.5 text-[13px] bg-primary text-white rounded-md hover:bg-primary-hover disabled:opacity-50 flex items-center gap-1"
        >
          {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
          立即执行
        </button>
        <button
          onClick={handleToggleEnabled}
          className="px-3 py-1.5 text-[13px] border border-border rounded-md hover:bg-muted flex items-center gap-1"
        >
          {rule.enabled ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
          {rule.enabled ? "暂停" : "启用"}
        </button>
        <button
          onClick={() => navigate(`/insights/rules/edit?id=${rule.id}`)}
          className="px-3 py-1.5 text-[13px] border border-border rounded-md hover:bg-muted flex items-center gap-1"
        >
          <Pencil className="w-3.5 h-3.5" />
          编辑
        </button>
        <button
          onClick={handleDelete}
          className="px-3 py-1.5 text-[13px] border border-border rounded-md text-danger hover:bg-red-50 flex items-center gap-1 ml-auto"
        >
          <Trash2 className="w-3.5 h-3.5" />
          删除
        </button>
      </div>

      {/* Run Result Banner */}
      {runResult && (
        <div
          className={`mb-4 flex items-start gap-2 px-4 py-3 rounded-md text-[13px] ${
            runResult.includes("失败") || runResult.includes("error")
              ? "bg-danger-light text-danger"
              : "bg-success-light text-success"
          }`}
        >
          {runResult.includes("失败") ? (
            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          ) : (
            <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
          )}
          <span>{runResult}</span>
        </div>
      )}

      {/* Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-5">
        <InfoCard icon={Database} label="数据源" value={rule.datasourceName || rule.datasourceId} />
        <InfoCard
          icon={Calendar}
          label="调度"
          value={`${rule.schedule === "daily" ? "每日" : "每周一"} ${rule.scheduleTime}`}
        />
        <InfoCard
          icon={Activity}
          label="上次执行"
          value={
            rule.lastRunAt
              ? new Date(rule.lastRunAt).toLocaleString("zh-CN")
              : "—"
          }
          subValue={
            lastStatus ? (
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded ${
                  lastStatus === "success"
                    ? "bg-success-light text-success"
                    : lastStatus === "failed"
                      ? "bg-danger-light text-danger"
                      : "bg-muted text-muted-foreground"
                }`}
              >
                {lastStatus}
              </span>
            ) : null
          }
        />
      </div>

      {/* Query Config */}
      <div className="bg-white border border-border-light rounded-lg p-5 mb-5">
        <h3 className="text-[13px] font-semibold mb-3">查询配置</h3>
        <div className="grid grid-cols-2 gap-3 text-[12px]">
          <InfoRow label="表" value={qc.table} />
          <InfoRow label="时间字段" value={qc.timeField} />
          <InfoRow label="时间范围" value={`${qc.timeRangeDays} 天`} />
          <InfoRow label="维度" value={qc.dimensions?.join(", ") || "—"} />
          <InfoRow
            label="度量"
            value={
              qc.measures?.map((m) => `${m.agg}(${m.field})`).join(", ") || "—"
            }
          />
          <InfoRow
            label="检测算法"
            value={rule.detectTypes.join(", ")}
          />
        </div>
      </div>

      {/* Records */}
      <div className="bg-white border border-border-light rounded-lg p-5">
        <h3 className="text-[13px] font-semibold mb-3 flex items-center gap-2">
          <FileText className="w-4 h-4" />
          执行记录
        </h3>
        {records.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground text-[12px]">
            还没有执行记录，点上方"立即执行"试试
          </div>
        ) : (
          <div className="space-y-2">
            {records.map((r) => (
              <div
                key={r.id}
                onClick={() => navigate(`/insights/records/${r.id}`)}
                className="flex items-center justify-between p-3 border border-border-light rounded-md cursor-pointer hover:border-primary transition-colors"
              >
                <div className="flex items-center gap-3">
                  {r.status === "success" ? (
                    <CheckCircle2 className="w-4 h-4 text-success flex-shrink-0" />
                  ) : r.status === "failed" ? (
                    <XCircle className="w-4 h-4 text-danger flex-shrink-0" />
                  ) : (
                    <Loader2 className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                  )}
                  <div>
                    <div className="text-[13px]">
                      {r.runAt ? new Date(r.runAt).toLocaleString("zh-CN") : "—"}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      周期: {r.periodStart?.slice(0, 10)} ~ {r.periodEnd?.slice(0, 10)} · 异常: {r.anomalyCount ?? 0}
                    </div>
                  </div>
                </div>
                <div className="text-[11px] text-muted-foreground">查看 →</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function InfoCard({
  icon: Icon,
  label,
  value,
  subValue,
}: {
  icon: typeof Database;
  label: string;
  value: string;
  subValue?: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-border-light rounded-lg p-4">
      <div className="flex items-center gap-2 text-muted-foreground text-[11px] mb-1">
        <Icon className="w-3.5 h-3.5" />
        {label}
      </div>
      <div className="text-[13px] font-medium truncate">{value}</div>
      {subValue && <div className="mt-1">{subValue}</div>}
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-muted-foreground">{label}: </span>
      <span className="text-foreground font-medium">{value}</span>
    </div>
  );
}
