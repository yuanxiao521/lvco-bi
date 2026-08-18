import { useState, useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Lightbulb,
  Database,
  ArrowLeft,
  ArrowRight,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Calendar,
  Activity,
  Save,
  Search,
} from "lucide-react";
import { useQuery } from "../../hooks/useQuery";
import { listDatasources, getDatasource, listDatasourceTables } from "../../api/datasources";
import {
  createInsightRule,
  getInsightRule,
  type CreateInsightRuleBody,
  type InsightRule,
} from "../../api/insights";
import type { DataSource, SchemaField } from "../../api/types";

type Step = 1 | 2 | 3 | 4;

interface DraftState {
  datasourceId: string | null;
  datasourceName: string;
  table: string;
  timeField: string;
  measureField: string;
  agg: "SUM" | "AVG" | "MAX" | "MIN" | "COUNT";
  dimensionField: string;
  timeRangeDays: number;
  detectTypes: string[];
  schedule: "daily" | "weekly";
  scheduleTime: string;
  name: string;
  description: string;
  enabled: boolean;
}

const DEFAULT_DRAFT: DraftState = {
  datasourceId: null,
  datasourceName: "",
  table: "",
  timeField: "",
  measureField: "",
  agg: "SUM",
  dimensionField: "",
  timeRangeDays: 30,
  detectTypes: ["z_score", "wow", "yoy", "moving_average"],
  schedule: "daily",
  scheduleTime: "09:00",
  name: "",
  description: "",
  enabled: true,
};

const DETECT_LABELS: Record<string, string> = {
  z_score: "Z-Score 异常",
  wow: "周环比",
  yoy: "同比",
  moving_average: "移动平均偏离",
};

/** 聚合方式对应的中文名 */
const AGG_LABELS: Record<string, string> = {
  SUM: "SUM 求和",
  AVG: "AVG 平均",
  MAX: "MAX 最大",
  MIN: "MIN 最小",
  COUNT: "COUNT 计数",
};

export default function RuleEditor() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const editId = params.get("id");
  const presetDatasourceId = params.get("datasourceId");
  const presetTable = params.get("table");

  const [step, setStep] = useState<Step>(1);
  const [draft, setDraft] = useState<DraftState>(DEFAULT_DRAFT);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 选中数据源后的 schema 字段
  const [schemaFields, setSchemaFields] = useState<SchemaField[]>([]);
  const [availableTables, setAvailableTables] = useState<string[]>([]);
  const [schemaLoading, setSchemaLoading] = useState(false);

  const datasourcesQuery = useQuery(() => listDatasources({}), []);
  const pgDatasources = (datasourcesQuery.data?.items ?? []).filter(
    (ds: DataSource) => ds.sourceType === "postgresql" && ds.status === "connected"
  );

  // 加载待编辑规则
  useEffect(() => {
    if (!editId) return;
    (async () => {
      try {
        const rule = await getInsightRule(editId);
        applyRuleToDraft(rule);
        // 编辑时也要加载 schema
        if (rule.datasourceId) {
          loadSchema(rule.datasourceId);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "加载洞察失败");
      }
    })();
  }, [editId]);

  // 预填数据源（从发现建议跳过来）
  useEffect(() => {
    if (presetDatasourceId && !editId) {
      const ds = pgDatasources.find((d) => d.id === presetDatasourceId);
      if (ds) {
        setDraft((d) => ({
          ...d,
          datasourceId: ds.id,
          datasourceName: ds.name,
          table: presetTable || d.table,
        }));
        loadSchema(presetDatasourceId);
        setStep(2);
      }
    }
  }, [presetDatasourceId, presetTable, pgDatasources, editId]);

  /** 加载数据源的 schema 字段 + 表列表 */
  async function loadSchema(datasourceId: string) {
    setSchemaLoading(true);
    try {
      const ds = await getDatasource(datasourceId);
      const fields = ds.schemaMeta?.fields ?? [];
      setSchemaFields(fields);
      // 自动预填表名
      const tableName = (ds.schemaMeta as Record<string, unknown> | null)?.["tableName"] as string | undefined;
      // 同时获取可用的表列表
      let tables: string[] = [];
      try {
        tables = await listDatasourceTables(datasourceId);
      } catch {
        // 表列表获取失败不阻塞
      }
      setAvailableTables(tables);
      setDraft((d) => ({
        ...d,
        table: tableName || (tables.length === 1 ? tables[0] : d.table),
      }));
    } catch {
      // schema 加载失败不阻塞流程
    } finally {
      setSchemaLoading(false);
    }
  }

  /** 选中数据源时 */
  function handleSelectDatasource(ds: DataSource) {
    setDraft((d) => ({
      ...d,
      datasourceId: ds.id,
      datasourceName: ds.name,
      table: "",
      timeField: "",
      measureField: "",
      dimensionField: "",
    }));
    loadSchema(ds.id);
    // 自动进入下一步
    setStep(2);
  }

  function applyRuleToDraft(rule: InsightRule) {
    const qc = rule.queryConfig || {};
    setDraft({
      datasourceId: rule.datasourceId,
      datasourceName: rule.datasourceName || "",
      table: qc.table || "",
      timeField: qc.timeField || "",
      measureField: qc.measures?.[0]?.field || "",
      agg: (qc.measures?.[0]?.agg as DraftState["agg"]) || "SUM",
      dimensionField: qc.dimensions?.[0] || "",
      timeRangeDays: qc.timeRangeDays || 30,
      detectTypes: rule.detectTypes || ["z_score"],
      schedule: (rule.schedule as "daily" | "weekly") || "daily",
      scheduleTime: rule.scheduleTime || "09:00",
      name: rule.name,
      description: rule.description || "",
      enabled: rule.enabled,
    });
    setStep(2);
  }

  function canGoNext(): boolean {
    if (step === 1) return Boolean(draft.datasourceId);
    if (step === 2) return Boolean(draft.table && draft.timeField && draft.measureField);
    if (step === 3) return draft.detectTypes.length > 0;
    if (step === 4) return Boolean(draft.name.trim() && draft.scheduleTime);
    return false;
  }

  async function handleSubmit() {
    if (!draft.datasourceId) {
      setError("请选择数据源");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const [hh, mm] = draft.scheduleTime.split(":").map((x) => parseInt(x, 10));
      const body: CreateInsightRuleBody = {
        datasourceId: draft.datasourceId,
        name: draft.name.trim(),
        description: draft.description.trim() || undefined,
        queryConfig: {
          table: draft.table,
          timeField: draft.timeField,
          measures: [{ field: draft.measureField, agg: draft.agg }],
          dimensions: draft.dimensionField ? [draft.dimensionField] : [],
          timeRangeDays: draft.timeRangeDays,
          filters: [],
        },
        detectTypes: draft.detectTypes,
        reportType: "daily_report",
        schedule: draft.schedule,
        scheduleTime: `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:00`,
        enabled: draft.enabled,
      };
      await createInsightRule(body);
      navigate("/insights");
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex-1 px-6 py-6 overflow-auto max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => navigate("/insights")}
          className="p-1.5 rounded-md hover:bg-muted text-muted-foreground"
          title="返回"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <Lightbulb className="w-5 h-5 text-primary" />
        <h1 className="text-[18px] font-semibold">
          {editId ? "编辑洞察" : "新建洞察"}
        </h1>
      </div>

      {/* Step Indicator */}
      <div className="flex items-center mb-6 bg-white border border-border-light rounded-lg p-2">
        <StepBadge step={1} active={step === 1} done={step > 1} label="选择数据源" />
        <StepConnector active={step > 1} />
        <StepBadge step={2} active={step === 2} done={step > 2} label="配置查询" />
        <StepConnector active={step > 2} />
        <StepBadge step={3} active={step === 3} done={step > 3} label="检测设置" />
        <StepConnector active={step > 3} />
        <StepBadge step={4} active={step === 4} done={false} label="调度与保存" />
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 flex items-start gap-2 px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">
          <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Step Content */}
      <div className="bg-white border border-border-light rounded-lg p-6">
        {step === 1 && (
          <Step1DataSource
            datasources={pgDatasources}
            loading={datasourcesQuery.loading}
            selectedId={draft.datasourceId}
            onSelect={handleSelectDatasource}
          />
        )}
        {step === 2 && (
          <Step2Query
            draft={draft}
            onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
            fields={schemaFields}
            tables={availableTables}
            loading={schemaLoading}
          />
        )}
        {step === 3 && (
          <Step3Detect
            draft={draft}
            onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
          />
        )}
        {step === 4 && (
          <Step4Schedule
            draft={draft}
            onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
          />
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between mt-4">
        <button
          onClick={() => setStep((s) => Math.max(1, s - 1) as Step)}
          disabled={step === 1}
          className="px-4 py-2 text-[13px] border border-border rounded-md hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed flex items-center gap-1"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          上一步
        </button>
        {step < 4 ? (
          <button
            onClick={() => canGoNext() && setStep((s) => Math.min(4, s + 1) as Step)}
            disabled={!canGoNext()}
            className="px-4 py-2 text-[13px] bg-primary text-white rounded-md hover:bg-primary-hover disabled:opacity-30 disabled:cursor-not-allowed flex items-center gap-1"
          >
            下一步
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!canGoNext() || submitting}
            className="px-4 py-2 text-[13px] bg-primary text-white rounded-md hover:bg-primary-hover disabled:opacity-30 disabled:cursor-not-allowed flex items-center gap-1"
          >
            {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            保存洞察
          </button>
        )}
      </div>
    </div>
  );
}

// ============ Step Badge ============
function StepBadge({ step, active, done, label }: { step: number; active: boolean; done: boolean; label: string }) {
  return (
    <div className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md transition-colors ${active ? "bg-primary-light text-primary" : "text-muted-foreground"}`}>
      <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-medium ${done ? "bg-primary text-white" : active ? "bg-primary text-white" : "bg-muted text-muted-foreground"}`}>
        {done ? <CheckCircle2 className="w-3.5 h-3.5" /> : step}
      </span>
      <span className="text-[12px] font-medium">{label}</span>
    </div>
  );
}

function StepConnector({ active }: { active: boolean }) {
  return <div className={`h-px w-8 ${active ? "bg-primary" : "bg-border"}`} />;
}

// ============ Step 1: 数据源选择 ============
function Step1DataSource({
  datasources,
  loading,
  selectedId,
  onSelect,
}: {
  datasources: DataSource[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (ds: DataSource) => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-[13px]">
        <Loader2 className="w-4 h-4 animate-spin" />
        加载数据源列表...
      </div>
    );
  }
  if (datasources.length === 0) {
    return (
      <div className="text-center py-12">
        <Database className="w-12 h-12 mx-auto text-muted-foreground/40 mb-3" />
        <p className="text-[14px] text-muted-foreground mb-1">还没有可用的 PostgreSQL 数据源</p>
        <p className="text-[12px] text-muted-foreground/70 mb-3">去源数据管理连接一个 PostgreSQL 数据库</p>
        <button
          onClick={() => { window.location.href = "/data-source"; }}
          className="px-4 py-2 text-[13px] bg-primary text-white rounded-md hover:bg-primary-hover"
        >
          前往源数据管理
        </button>
      </div>
    );
  }
  return (
    <div>
      <h3 className="text-[14px] font-semibold mb-1">选择数据源</h3>
      <p className="text-[12px] text-muted-foreground mb-4">系统将对该数据源的表进行异常检测分析</p>
      <div className="space-y-2">
        {datasources.map((ds) => (
          <div
            key={ds.id}
            onClick={() => onSelect(ds)}
            className={`flex items-center justify-between p-3 border rounded-md cursor-pointer transition-colors ${
              selectedId === ds.id
                ? "border-primary bg-primary-light/30"
                : "border-border-light hover:border-primary/50"
            }`}
          >
            <div className="flex items-center gap-2">
              <Database className="w-4 h-4 text-primary" />
              <div>
                <div className="text-[13px] font-medium">{ds.name}</div>
                <div className="text-[11px] text-muted-foreground">PostgreSQL · {ds.rowCount.toLocaleString()} 行</div>
              </div>
            </div>
            {selectedId === ds.id && <CheckCircle2 className="w-4 h-4 text-primary" />}
          </div>
        ))}
      </div>
    </div>
  );
}

// ============ Step 2: 查询配置（自动识别字段） ============
function Step2Query({
  draft,
  onChange,
  fields,
  tables,
  loading,
}: {
  draft: DraftState;
  onChange: (patch: Partial<DraftState>) => void;
  fields: SchemaField[];
  tables: string[];
  loading: boolean;
}) {
  const timeFields = fields.filter((f) => f.category === "time");
  const measureFields = fields.filter((f) => f.category === "measure");
  const dimensionFields = fields.filter((f) => f.category === "dimension");

  const hasSchema = fields.length > 0;
  const hasTables = tables.length > 0;
  const onlyOneTable = tables.length === 1;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[14px] font-semibold mb-1">第二步：配置要监控的数据</h3>
        <p className="text-[12px] text-muted-foreground">
          {hasSchema
            ? "系统已自动读取数据源结构，请确认你想监控哪张表的哪个数值"
            : "请填写数据库中的表名和列名（注意：要与数据库实际名称一致）"}
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-[13px] py-4">
          <Loader2 className="w-4 h-4 animate-spin" />
          正在读取数据源表结构和字段...
        </div>
      ) : (
        <>
          {/* 表名 */}
          {hasTables ? (
            onlyOneTable ? (
              <div className="p-4 rounded-md border border-emerald-200 bg-emerald-50/50">
                <div className="flex items-center gap-2 mb-1">
                  <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                  <span className="text-[13px] font-medium text-emerald-700">已自动识别监控表</span>
                </div>
                <p className="text-[12px] text-emerald-600/80">
                  此数据源只有 1 张表：<code className="px-1.5 py-0.5 bg-emerald-100 rounded text-[12px] font-mono">{tables[0]}</code>，已自动选中。如果有多个表，可选择不同的表。
                </p>
              </div>
            ) : (
              <Field label="要监控哪张表？" required hint="此数据库有多张表，请选择你想监控异常的那张">
                <FieldSelect
                  value={draft.table}
                  onChange={(v) => onChange({ table: v })}
                  options={tables.map((t) => ({ value: t, label: t }))}
                  placeholder="请选择表..."
                />
              </Field>
            )
          ) : (
            <Field label="表名" required hint="数据库中表的名称，如 orders、sales_daily">
              <input
                type="text"
                value={draft.table}
                onChange={(e) => onChange({ table: e.target.value })}
                placeholder="orders"
                className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
              />
            </Field>
          )}

          {/* 时间字段 */}
          <div className="grid grid-cols-2 gap-3">
            <Field label="数据里的时间列" required hint="哪一列记录的是时间？如订单日期 created_at">
              {timeFields.length > 0 ? (
                <FieldSelect
                  value={draft.timeField}
                  onChange={(v) => onChange({ timeField: v })}
                  options={timeFields.map((f) => ({
                    value: f.name,
                    label: `${f.displayName || f.name} (${f.dataType})`,
                  }))}
                  placeholder="选择时间列..."
                />
              ) : (
                <input
                  type="text"
                  value={draft.timeField}
                  onChange={(e) => onChange({ timeField: e.target.value })}
                  placeholder="如 created_at"
                  className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
                />
              )}
            </Field>
            <Field label="看最近多少天？" required hint="取最近N天的数据来检测异常，默认30天">
              <input
                type="number"
                min={1}
                max={365}
                value={draft.timeRangeDays}
                onChange={(e) => onChange({ timeRangeDays: parseInt(e.target.value) || 30 })}
                className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
              />
            </Field>
          </div>

          {/* 度量字段 + 聚合方式 */}
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <Field label="要监控的数值列" required hint="哪一列是你要监控的数字？如销售额 amount、数量 qty">
                {measureFields.length > 0 ? (
                  <FieldSelect
                    value={draft.measureField}
                    onChange={(v) => onChange({ measureField: v })}
                    options={measureFields.map((f) => ({
                      value: f.name,
                      label: `${f.displayName || f.name} (${f.dataType})`,
                    }))}
                    placeholder="选择数值列..."
                  />
                ) : (
                  <input
                    type="text"
                    value={draft.measureField}
                    onChange={(e) => onChange({ measureField: e.target.value })}
                    placeholder="amount"
                    className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
                  />
                )}
              </Field>
            </div>
            <Field label="怎么算？" required hint="对数值列做什么运算：求和、算平均、取最大/最小值">
              <select
                value={draft.agg}
                onChange={(e) => onChange({ agg: e.target.value as DraftState["agg"] })}
                className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
              >
                {Object.entries(AGG_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </Field>
          </div>

          {/* 维度字段 */}
          <Field label="按什么分组？（可选）" hint="想按地区、产品等维度分别检测吗？不选则对整表检测">
            {dimensionFields.length > 0 ? (
              <FieldSelect
                value={draft.dimensionField}
                onChange={(v) => onChange({ dimensionField: v })}
                options={[
                  { value: "", label: "不分组（默认）" },
                  ...dimensionFields.map((f) => ({
                    value: f.name,
                    label: `${f.displayName || f.name} (${f.dataType})`,
                  })),
                ]}
                placeholder="选择维度字段（可选）..."
              />
            ) : (
              <input
                type="text"
                value={draft.dimensionField}
                onChange={(e) => onChange({ dimensionField: e.target.value })}
                placeholder="category（可选）"
                className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
              />
            )}
          </Field>

          {/* 字段快速浏览 */}
          {hasSchema && (
            <div className="bg-slate-50 rounded-md p-3">
              <div className="text-[11px] text-muted-foreground font-medium mb-2">
                <Search className="w-3 h-3 inline -mt-0.5 mr-1" />
                数据源字段总览（已自动分类）
              </div>
              <div className="grid grid-cols-3 gap-2 text-[11px]">
                <FieldGroup label="时间" color="text-blue-600" items={timeFields} />
                <FieldGroup label="度量" color="text-emerald-600" items={measureFields} />
                <FieldGroup label="维度" color="text-amber-600" items={dimensionFields} />
              </div>
            </div>
          )}

          <div className="bg-info-light px-3 py-2 rounded-md text-[12px] text-info flex items-start gap-2">
            <Activity className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>
              {hasSchema
                ? "提示：请确认字段选择无误。系统会基于此构造 SQL 查询并定时执行异常检测。"
                : "提示：字段名必须与数据库中实际的列名一致（snake_case），系统会基于此构造 SQL 查询"}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function FieldGroup({ label, color, items }: { label: string; color: string; items: SchemaField[] }) {
  return (
    <div>
      <span className={`font-medium ${color}`}>{label} ({items.length})</span>
      <ul className="mt-0.5 space-y-0.5 text-muted-foreground">
        {items.slice(0, 6).map((f) => (
          <li key={f.name} className="truncate" title={f.name}>{f.name}</li>
        ))}
        {items.length > 6 && <li className="text-muted-foreground/60">+{items.length - 6} 更多...</li>}
      </ul>
    </div>
  );
}

// ============ 通用下拉（带搜索高亮） ============
function FieldSelect({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
    >
      <option value="" disabled>{placeholder}</option>
      {options.map((opt) => (
        <option key={opt.value} value={opt.value}>{opt.label}</option>
      ))}
    </select>
  );
}

// ============ Step 3: 检测设置 ============
function Step3Detect({
  draft,
  onChange,
}: {
  draft: DraftState;
  onChange: (patch: Partial<DraftState>) => void;
}) {
  function toggleDetectType(t: string) {
    const has = draft.detectTypes.includes(t);
    onChange({
      detectTypes: has ? draft.detectTypes.filter((x) => x !== t) : [...draft.detectTypes, t],
    });
  }
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[14px] font-semibold mb-1">异常检测算法</h3>
        <p className="text-[12px] text-muted-foreground">选择要应用的检测方法（可多选），每种方法会自动判定异常阈值</p>
      </div>

      <div className="space-y-2">
        {Object.entries(DETECT_LABELS).map(([key, label]) => {
          const checked = draft.detectTypes.includes(key);
          return (
            <div
              key={key}
              onClick={() => toggleDetectType(key)}
              className={`flex items-center gap-3 p-3 border rounded-md cursor-pointer transition-colors ${
                checked ? "border-primary bg-primary-light/20" : "border-border-light hover:border-primary/50"
              }`}
            >
              <input type="checkbox" checked={checked} onChange={() => toggleDetectType(key)} className="w-4 h-4" />
              <div className="flex-1">
                <div className="text-[13px] font-medium">{label}</div>
                <div className="text-[11px] text-muted-foreground">
                  {key === "z_score" && "基于标准差识别偏离均值的数据点（适合正态分布数据）"}
                  {key === "wow" && "与上周同期对比，超过阈值则告警（适合有周周期规律的数据）"}
                  {key === "yoy" && "与去年同期对比，识别长期趋势变化"}
                  {key === "moving_average" && "与移动平均线偏离程度（适合平滑波动数据）"}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ============ Step 4: 调度与保存 ============
function Step4Schedule({
  draft,
  onChange,
}: {
  draft: DraftState;
  onChange: (patch: Partial<DraftState>) => void;
}) {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[14px] font-semibold mb-1">调度与基础信息</h3>
        <p className="text-[12px] text-muted-foreground">设置执行频率、触发时间和洞察名称</p>
      </div>

      <Field label="洞察名称" required>
        <input
          type="text"
          value={draft.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="例如: 每日订单金额异常监控"
          className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
        />
      </Field>

      <Field label="描述（可选）">
        <textarea
          value={draft.description}
          onChange={(e) => onChange({ description: e.target.value })}
          rows={2}
          placeholder="说明这条洞察监控什么、什么情况下应该被关注"
          className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none resize-none"
        />
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="执行频率" required>
          <select
            value={draft.schedule}
            onChange={(e) => onChange({ schedule: e.target.value as "daily" | "weekly" })}
            className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
          >
            <option value="daily">每日</option>
            <option value="weekly">每周一</option>
          </select>
        </Field>
        <Field label="执行时间" required>
          <input
            type="time"
            value={draft.scheduleTime}
            onChange={(e) => onChange({ scheduleTime: e.target.value })}
            className="w-full px-3 py-2 text-[13px] border border-border-light rounded-md bg-white focus:border-primary focus:ring-1 focus:ring-ring outline-none"
          />
        </Field>
      </div>

      <div className="flex items-center gap-2 p-3 border border-border-light rounded-md">
        <input
          type="checkbox"
          id="rule-enabled"
          checked={draft.enabled}
          onChange={(e) => onChange({ enabled: e.target.checked })}
          className="w-4 h-4"
        />
        <label htmlFor="rule-enabled" className="text-[13px] cursor-pointer">
          立即启用
        </label>
        <span className="text-[11px] text-muted-foreground ml-auto">
          <Calendar className="w-3.5 h-3.5 inline -mt-0.5 mr-1" />
          {draft.schedule === "daily" ? "每天" : "每周一"} {draft.scheduleTime} 执行
        </span>
      </div>

      <div className="bg-success-light px-3 py-2 rounded-md text-[12px] text-success flex items-start gap-2">
        <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
        <span>
          启用后，系统会在 {draft.schedule === "daily" ? "每天" : "每周一"} {draft.scheduleTime} 自动执行检测，
          异常结果将通过通知中心推送，并生成日报可随时查看。
        </span>
      </div>
    </div>
  );
}

// ============ Form Field Wrapper ============
function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-[12px] font-medium text-foreground mb-1">
        {label}
        {required && <span className="text-danger ml-0.5">*</span>}
      </label>
      {children}
      {hint && <p className="text-[11px] text-muted-foreground mt-1">{hint}</p>}
    </div>
  );
}
