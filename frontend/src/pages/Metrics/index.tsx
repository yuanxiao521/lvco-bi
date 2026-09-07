import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  GitBranch,
  Network,
  Activity,
  Plus,
  Loader2,
  RotateCcw,
  Rocket,
  Search,
  X,
  Layers,
} from "lucide-react";
import {
  listMetrics,
  createMetric,
  publishMetricVersion,
  rollbackMetricVersion,
} from "../../api/metrics";
import { listDatasources, getDatasource } from "../../api/datasources";
import type { DataSource, SchemaField } from "../../api/types";
import type {
  MetricCreatePayload,
  MetricDefinition,
  MetricFormulaType,
} from "../../types/metric";

interface MetricModalProps {
  title: string;
  subtitle?: string;
  onClose: () => void;
  onSubmit: (value: string) => void;
  submitting: boolean;
  placeholder?: string;
  actionLabel: string;
  error?: string | null;
}

function MetricModal({
  title,
  subtitle,
  onClose,
  onSubmit,
  submitting,
  placeholder,
  actionLabel,
  error,
}: MetricModalProps) {
  const [value, setValue] = useState("");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-[12px] shadow-lg w-full max-w-[420px] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-[15px] font-semibold text-foreground">{title}</h3>
          <button onClick={onClose} className="p-1 text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        {subtitle && <p className="text-[12px] text-muted-foreground mb-3">{subtitle}</p>}
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={placeholder}
          className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        />
        {error && <div className="mt-2 px-3 py-2 rounded-md bg-danger-light text-danger text-[12px]">{error}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-[13px] font-medium text-muted-foreground hover:bg-muted transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => onSubmit(value)}
            disabled={submitting || !value.trim()}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 transition-colors"
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

const AGG_OPTIONS = [
  { value: "SUM", label: "求和" },
  { value: "AVG", label: "平均" },
  { value: "COUNT", label: "计数" },
  { value: "COUNT_DISTINCT", label: "去重计数" },
  { value: "MAX", label: "最大值" },
  { value: "MIN", label: "最小值" },
  { value: "MEDIAN", label: "中位数" },
  { value: "STDDEV", label: "标准差" },
];

type BuilderMode = "simple" | "advanced";

function CreateMetricModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [mode, setMode] = useState<BuilderMode>("simple");
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [fields, setFields] = useState<SchemaField[]>([]);
  const [form, setForm] = useState({
    key: "",
    name: "",
    formula: "",
    datasource_id: "",
    source_field: "",
    agg: "SUM",
  });
  const [submitting, setSubmitting] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((p) => ({ ...p, [k]: e.target.value }));

  // 简单模式：加载数据源，选中后拉取其字段
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await listDatasources({ pageSize: 100 });
        if (!cancelled) setDatasources(res.items ?? []);
      } catch {
        if (!cancelled) setDatasources([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!form.datasource_id) {
      setFields([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const ds = await getDatasource(form.datasource_id);
        if (!cancelled) setFields(ds.schemaMeta?.fields ?? []);
      } catch {
        if (!cancelled) setFields([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [form.datasource_id]);

  const previewFormula = `${
    form.agg && form.source_field ? `${form.agg}("${form.source_field}")` : ""
  }`;

  const submit = async () => {
    setSubmitting(true);
    try {
      const payload: MetricCreatePayload = {
        key: form.key,
        name: form.name,
      };
      if (mode === "simple" && form.datasource_id && form.source_field && form.agg) {
        payload.datasourceId = form.datasource_id;
        payload.sourceField = form.source_field;
        payload.agg = form.agg;
      } else {
        payload.formula = form.formula;
        if (form.datasource_id) payload.datasourceId = form.datasource_id;
      }
      await createMetric(payload);
      onCreated();
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  const inputCls =
    "w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring";

  const canSubmit =
    form.key.trim() &&
    form.name.trim() &&
    (mode === "simple"
      ? Boolean(form.datasource_id && form.source_field && form.agg)
      : Boolean(form.formula.trim()));

  // 依据公式是否引用其他指标自动判定类型（与后端 create 推断逻辑一致）
  const effectiveFormula =
    mode === "simple" ? previewFormula : form.formula;
  const isDerived = /metric\s*\(\s*['"]/.test(effectiveFormula);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-[12px] shadow-lg w-full max-w-[520px] p-5 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-[15px] font-semibold text-foreground">新建指标</h3>
          <button onClick={onClose} className="p-1 text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* 构建方式切换 */}
        <div className="flex items-center gap-1 mb-4 bg-muted rounded-lg p-1 w-fit">
          <button
            type="button"
            onClick={() => setMode("simple")}
            className={`px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors ${
              mode === "simple" ? "bg-white shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            简单模式（选字段）
          </button>
          <button
            type="button"
            onClick={() => setMode("advanced")}
            className={`px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors ${
              mode === "advanced" ? "bg-white shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            高级模式（写公式）
          </button>
        </div>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[12px] text-muted-foreground mb-1">指标名称</label>
              <input className={inputCls} value={form.name} onChange={set("name")} placeholder="e.g. 总营收" />
            </div>
            <div>
              <label className="block text-[12px] text-muted-foreground mb-1">标识 Key</label>
              <input className={inputCls} value={form.key} onChange={set("key")} placeholder="e.g. revenue" />
            </div>
          </div>

          {mode === "simple" ? (
            <>
              <div>
                <label className="block text-[12px] text-muted-foreground mb-1">选择数据源</label>
                <select
                  className={inputCls}
                  value={form.datasource_id}
                  onChange={(e) => {
                    setForm((p) => ({ ...p, datasource_id: e.target.value, source_field: "" }));
                  }}
                >
                  <option value="">选择数据源</option>
                  {datasources.map((ds) => (
                    <option key={ds.id} value={ds.id}>{ds.name}</option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[12px] text-muted-foreground mb-1">字段</label>
                  <select className={inputCls} value={form.source_field} onChange={set("source_field")}>
                    <option value="">选择字段</option>
                    {fields.map((f) => (
                      <option key={f.name} value={f.name}>{f.displayName || f.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-[12px] text-muted-foreground mb-1">聚合方式</label>
                  <select className={inputCls} value={form.agg} onChange={set("agg")}>
                    {AGG_OPTIONS.map((a) => (
                      <option key={a.value} value={a.value}>{a.label} · {a.value}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-[12px] text-muted-foreground mb-1">生成的口径公式（自动）</label>
                <div className="px-3 py-2 text-[13px] font-mono rounded-lg bg-muted text-card-foreground">
                  {previewFormula || "请先选择数据源、字段和聚合方式"}
                </div>
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="block text-[12px] text-muted-foreground mb-1">计算公式</label>
                <input className={inputCls} value={form.formula} onChange={set("formula")} placeholder="e.g. SUM(amount)" />
              </div>
              <div>
                <label className="block text-[12px] text-muted-foreground mb-1">绑定数据源（可选，空为模板指标）</label>
                <select
                  className={inputCls}
                  value={form.datasource_id}
                  onChange={(e) => setForm((p) => ({ ...p, datasource_id: e.target.value }))}
                >
                  <option value="">不绑定（模板指标）</option>
                  {datasources.map((ds) => (
                    <option key={ds.id} value={ds.id}>{ds.name}</option>
                  ))}
                </select>
              </div>
            </>
          )}

          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">类型（自动识别）</label>
            <div
              className={`px-3 py-2 text-[13px] rounded-lg flex items-center gap-2 ${
                isDerived
                  ? "bg-ai-light text-ai"
                  : "bg-primary-light text-primary"
              }`}
            >
              {isDerived ? (
                <>
                  <GitBranch className="w-3.5 h-3.5" />
                  派生指标（公式引用其他指标，将纳入血缘追踪）
                </>
              ) : (
                <>
                  <Layers className="w-3.5 h-3.5" />
                  基础指标（对原始字段做聚合）
                </>
              )}
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-[13px] font-medium text-muted-foreground hover:bg-muted transition-colors">
            取消
          </button>
          <button
            onClick={submit}
            disabled={submitting || !canSubmit}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 transition-colors"
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            创建指标
          </button>
        </div>
      </div>
    </div>
  );
}

type ModalState =
  | { kind: "publish"; metric: MetricDefinition }
  | { kind: "rollback"; metric: MetricDefinition }
  | { kind: "create" }
  | null;

export default function MetricsPage() {
  const navigate = useNavigate();
  const [metrics, setMetrics] = useState<MetricDefinition[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<ModalState>(null);
  const [modalError, setModalError] = useState<string | null>(null);
  const [mode, setMode] = useState<"basic" | "derived" | "">("");
  const [submitting, setSubmitting] = useState(false);

  const fetchList = async () => {
    setLoading(true);
    setError(null);
    try {
      const items = await listMetrics({
        page,
        pageSize,
        search: search || undefined,
        formula_type: mode || undefined,
      });
      // 后端当前返回非分页数组，items/total 直接取数组内容
      setMetrics(items ?? []);
      setTotal(Array.isArray(items) ? items.length : 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : "获取指标列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchList();
  }, [page, mode]);

  // 打开/切换弹窗时清空上次的错误提示
  useEffect(() => {
    setModalError(null);
  }, [modal]);

  const runModal = async (value: string) => {
    if (!modal || modal.kind === "create") return;
    setSubmitting(true);
    setModalError(null);
    try {
      if (modal.kind === "publish") {
        await publishMetricVersion(modal.metric.id, value);
      } else {
        await rollbackMetricVersion(modal.metric.id, value);
      }
      setModal(null);
      fetchList();
    } catch (err) {
      setModalError(err instanceof Error ? err.message : "操作失败，请检查输入是否符合要求");
    } finally {
      setSubmitting(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const badge = (t: MetricFormulaType) =>
    t === "derived"
      ? "bg-ai-light text-ai"
      : "bg-primary-light text-primary";

  return (
    <div className="flex-1 p-6 space-y-5">
      <header className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
          <Network className="w-5 h-5 text-primary" />
        </div>
        <div>
          <h1 className="text-[17px] font-semibold text-foreground">指标中心</h1>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            统一管理指标定义，追踪血缘依赖、影响分析与版本治理
          </p>
        </div>
      </header>

      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 text-[12px] border border-border rounded-lg bg-white px-2 py-1.5">
            <Search className="w-3.5 h-3.5 text-muted-foreground" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setPage(1);
                  fetchList();
                }
              }}
              placeholder="搜索指标名称 / Key"
              className="bg-transparent outline-none text-[13px] text-card-foreground w-[220px]"
            />
          </div>
          <select
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as typeof mode);
              setPage(1);
            }}
            className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            <option value="">全部类型</option>
            <option value="basic">基础指标</option>
            <option value="derived">派生指标</option>
          </select>
        </div>
        <button
          onClick={() => setModal({ kind: "create" })}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors"
        >
          <Plus className="w-4 h-4" />
          新建指标
        </button>
      </div>

      {error && (
        <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">{error}</div>
      )}

      <div className="bg-white rounded-[10px] shadow-card border border-border-light overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-[13px] min-w-[760px]">
            <thead>
              <tr className="bg-muted">
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">名称</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">Key</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">类型</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">数据源</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">计算公式</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">版本</th>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-[12px]">操作</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={7} className="py-14 text-center text-[13px] text-muted-foreground">
                    <span className="inline-flex items-center gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      正在加载指标...
                    </span>
                  </td>
                </tr>
              ) : metrics.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-14 text-center text-[13px] text-muted-foreground">
                    暂无指标，点击右上角「新建指标」开始
                  </td>
                </tr>
              ) : (
                metrics.map((m) => (
                  <tr key={m.id} className="border-t border-border-light hover:bg-muted/40">
                    <td className="px-4 py-3 font-medium text-foreground">{m.name}</td>
                    <td className="px-4 py-3 text-muted-foreground font-mono text-[12px]">{m.key}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex px-2 py-0.5 rounded text-[11px] font-medium ${badge(m.formulaType)}`}>
                        {m.formulaType === "derived" ? "派生" : "基础"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {m.datasourceId ? (
                        <span className="inline-flex px-2 py-0.5 rounded text-[11px] font-medium bg-success-light text-success">
                          已绑定数据源
                        </span>
                      ) : (
                        <span className="inline-flex px-2 py-0.5 rounded text-[11px] font-medium bg-muted text-muted-foreground" title="模板指标可绑定到任意同结构数据源，需先指定字段才能解析">
                          模板指标
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground text-[12px] font-mono max-w-[220px] truncate" title={m.formula}>
                      {m.formula || "—"}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground font-mono text-[12px]">{m.version}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => navigate(`/metrics/${m.id}?tab=lineage`)}
                          title="查看血缘"
                          className="p-1.5 rounded-md text-muted-foreground hover:text-primary hover:bg-primary-light transition-colors"
                        >
                          <GitBranch className="w-4 h-4" />
                        </button>
                        <button
                          onClick={() => navigate(`/metrics/${m.id}?tab=impact`)}
                          title="影响分析"
                          className="p-1.5 rounded-md text-muted-foreground hover:text-chart-6 hover:bg-[#F3F0FF] transition-colors"
                        >
                          <Activity className="w-4 h-4" />
                        </button>
                        <button
                          onClick={() => setModal({ kind: "publish", metric: m })}
                          title="发布版本"
                          className="p-1.5 rounded-md text-muted-foreground hover:text-chart-2 hover:bg-success-light transition-colors"
                        >
                          <Rocket className="w-4 h-4" />
                        </button>
                        <button
                          onClick={() => setModal({ kind: "rollback", metric: m })}
                          title="回滚版本"
                          className="p-1.5 rounded-md text-muted-foreground hover:text-danger hover:bg-danger-light transition-colors"
                        >
                          <RotateCcw className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {!loading && total > pageSize && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-border-light">
            <span className="text-[12px] text-muted-foreground">
              共 {total} 条 · 第 {page}/{totalPages} 页
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="px-3 py-1.5 rounded-lg border border-border text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50 transition-colors"
              >
                上一页
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="px-3 py-1.5 rounded-lg border border-border text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50 transition-colors"
              >
                下一页
              </button>
            </div>
          </div>
        )}
      </div>

      {modal?.kind === "create" && <CreateMetricModal onClose={() => setModal(null)} onCreated={() => fetchList()} />}
      {modal?.kind === "publish" && (
        <MetricModal
          title={`发布新版本：${modal.metric.name}`}
          subtitle="新版本将基于当前指标状态快照生成"
          onClose={() => setModal(null)}
          onSubmit={runModal}
          submitting={submitting}
          placeholder="填写变更说明（change note）"
          actionLabel="发布"
          error={modalError}
        />
      )}
      {modal?.kind === "rollback" && (
        <MetricModal
          title={`回滚版本：${modal.metric.name}`}
          subtitle="输入目标版本号（数字，如 2）以回滚"
          onClose={() => setModal(null)}
          onSubmit={runModal}
          submitting={submitting}
          placeholder="目标版本号，如 2"
          actionLabel="回滚"
          error={modalError}
        />
      )}
    </div>
  );
}