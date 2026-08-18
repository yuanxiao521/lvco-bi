import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Lightbulb,
  Plus,
  RefreshCw,
  Database,
  Sparkles,
  Loader2,
  CheckCircle2,
  AlertCircle,
  ChevronRight,
  FileText,
} from "lucide-react";
import { useQuery } from "../../hooks/useQuery";
import {
  listInsightRules,
  listInsightSuggestions,
  discoverDatasource,
  acceptSuggestion,
  dismissSuggestion,
  type InsightRule,
  type InsightSuggestion,
} from "../../api/insights";
import { listDatasources } from "../../api/datasources";
import type { DataSource } from "../../api/types";

export default function InsightsIndexPage() {
  const [activeTab, setActiveTab] = useState<"rules" | "suggestions">("rules");

  const rulesQuery = useQuery(() => listInsightRules({}), []);
  const suggestionsQuery = useQuery(() => listInsightSuggestions({}), []);
  const datasourcesQuery = useQuery(() => listDatasources({}), []);

  const pgDatasources = useMemo(() => {
    const items = datasourcesQuery.data?.items ?? [];
    return items.filter(
      (ds: DataSource) =>
        ds.sourceType === "postgresql" && ds.status === "connected"
    );
  }, [datasourcesQuery.data]);

  const rules = rulesQuery.data?.items ?? [];
  const suggestions = suggestionsQuery.data?.items ?? [];
  const pendingSuggestions = suggestions.filter((s: InsightSuggestion) => s.status === "pending");

  return (
    <div className="flex-1 px-6 py-6 overflow-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Lightbulb className="w-6 h-6 text-primary" />
          <div>
            <h1 className="text-[20px] font-semibold">智能洞察</h1>
            <p className="text-[12px] text-muted-foreground mt-0.5">
              自动扫描数据库 · 每日生成日报 · 异常实时推送
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Link
            to="/report-center?sourceType=ai_insight"
            className="px-3 py-1.5 rounded-md border border-border hover:bg-muted flex items-center gap-1.5 text-[13px]"
          >
            <FileText className="w-3.5 h-3.5" />
            AI日报
          </Link>
          <button
            onClick={() => {
              rulesQuery.refetch();
              suggestionsQuery.refetch();
              datasourcesQuery.refetch();
            }}
            className="px-3 py-1.5 rounded-md border border-border hover:bg-muted flex items-center gap-1.5 text-[13px]"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </button>
          <Link
            to="/insights/rules/new"
            className="px-3 py-1.5 rounded-md bg-primary text-white hover:bg-primary-hover flex items-center gap-1.5 text-[13px]"
          >
            <Plus className="w-3.5 h-3.5" />
            新建洞察
          </Link>
        </div>
      </div>

      {/* 数据源发现区 - 直接复用源数据管理里已连接的 PostgreSQL */}
      <DatasourceDiscoverySection
        pgDatasources={pgDatasources}
        loading={datasourcesQuery.loading}
        onDiscovered={() => {
          suggestionsQuery.refetch();
          rulesQuery.refetch();
        }}
      />

      {/* Tab 切换 */}
      <div className="flex items-center gap-1 border-b border-border-light mb-5 mt-6">
        <TabButton
          active={activeTab === "rules"}
          onClick={() => setActiveTab("rules")}
          label="我的洞察"
          count={rules.length}
        />
        <TabButton
          active={activeTab === "suggestions"}
          onClick={() => setActiveTab("suggestions")}
          label="待处理建议"
          count={pendingSuggestions.length}
          highlight={pendingSuggestions.length > 0}
        />
      </div>

      {/* 内容区 */}
      {activeTab === "rules" ? (
        <RulesList rules={rules} loading={rulesQuery.loading} />
      ) : (
        <SuggestionsList
          suggestions={suggestions}
          loading={suggestionsQuery.loading}
          onActed={() => {
            suggestionsQuery.refetch();
            rulesQuery.refetch();
          }}
        />
      )}
    </div>
  );
}

// ============ 数据源发现区 ============
function DatasourceDiscoverySection({
  pgDatasources,
  loading,
  onDiscovered,
}: {
  pgDatasources: DataSource[];
  loading: boolean;
  onDiscovered: () => void;
}) {
  const [discoveringId, setDiscoveringId] = useState<string | null>(null);
  const [discoveredCount, setDiscoveredCount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleDiscover = async (ds: DataSource) => {
    setDiscoveringId(ds.id);
    setError(null);
    setDiscoveredCount(null);
    try {
      const result = await discoverDatasource(ds.id);
      setDiscoveredCount(result.suggestionsCreated);
      onDiscovered();
    } catch (e) {
      setError(e instanceof Error ? e.message : "扫描失败");
    } finally {
      setDiscoveringId(null);
    }
  };

  if (loading) {
    return (
      <div className="bg-gradient-to-br from-primary/5 to-ai/5 border border-primary/20 rounded-lg p-5 mb-4">
        <div className="flex items-center gap-2 text-muted-foreground text-[13px]">
          <Loader2 className="w-4 h-4 animate-spin" />
          加载已连接数据源...
        </div>
      </div>
    );
  }

  if (pgDatasources.length === 0) {
    return (
      <div className="bg-gradient-to-br from-muted/50 to-muted/30 border border-border-light rounded-lg p-5 mb-4">
        <div className="flex items-start gap-3">
          <Database className="w-5 h-5 text-muted-foreground flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <h3 className="text-[13px] font-medium text-foreground mb-1">
              还没有可扫描的 PostgreSQL 数据源
            </h3>
            <p className="text-[12px] text-muted-foreground mb-2">
              去源数据管理连接一个 PostgreSQL 数据库，系统会自动发现可监控的表
            </p>
            <Link
              to="/data-source"
              className="inline-flex items-center gap-1 text-[12px] text-primary hover:underline"
            >
              前往源数据管理
              <ChevronRight className="w-3 h-3" />
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-gradient-to-br from-primary/5 to-ai/5 border border-primary/20 rounded-lg p-5 mb-4">
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="w-4 h-4 text-primary" />
        <h3 className="text-[13px] font-medium text-foreground">
          已连接的数据源（点击一键扫描生成监控建议）
        </h3>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {pgDatasources.map((ds, idx) => (
          <div
            key={ds.id}
            className={`animate-slide-up stagger-${idx + 1} bg-white border border-border-light rounded-md p-3 flex items-center justify-between hover:shadow-sm transition-shadow`}
          >
            <div className="flex items-center gap-2 min-w-0">
              <Database className="w-4 h-4 text-primary flex-shrink-0" />
              <div className="min-w-0">
                <div className="text-[13px] font-medium truncate">{ds.name}</div>
                <div className="text-[11px] text-muted-foreground">
                  PostgreSQL · {ds.rowCount.toLocaleString()} 行
                </div>
              </div>
            </div>
            <button
              onClick={() => handleDiscover(ds)}
              disabled={discoveringId === ds.id}
              className="px-2.5 py-1 text-[12px] bg-primary text-white rounded hover:bg-primary-hover disabled:opacity-50 flex items-center gap-1 flex-shrink-0"
            >
              {discoveringId === ds.id ? (
                <>
                  <Loader2 className="w-3 h-3 animate-spin" />
                  扫描中
                </>
              ) : (
                <>
                  <Sparkles className="w-3 h-3" />
                  发现
                </>
              )}
            </button>
          </div>
        ))}
      </div>
      {discoveredCount !== null && (
        <div className="mt-3 flex items-center gap-2 text-[12px] text-success">
          <CheckCircle2 className="w-4 h-4" />
          发现 {discoveredCount} 条监控建议，请在下方"待处理建议"中查看
        </div>
      )}
      {error && (
        <div className="mt-3 flex items-center gap-2 text-[12px] text-danger">
          <AlertCircle className="w-4 h-4" />
          {error}
        </div>
      )}
    </div>
  );
}

// ============ Tab 按钮 ============
function TabButton({
  active,
  onClick,
  label,
  count,
  highlight = false,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  highlight?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-4 py-2.5 text-[13px] font-medium transition-colors border-b-2 -mb-px flex items-center gap-2 ${
        active
          ? "text-primary border-primary"
          : "text-muted-foreground border-transparent hover:text-card-foreground"
      }`}
    >
      {label}
      <span
        className={`text-[10px] px-1.5 py-0.5 rounded-full ${
          highlight
            ? "bg-danger text-white"
            : active
              ? "bg-primary-light text-primary"
              : "bg-muted text-muted-foreground"
        }`}
      >
        {count}
      </span>
    </button>
  );
}

// ============ 规则列表 ============
function RulesList({ rules, loading }: { rules: InsightRule[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="text-muted-foreground text-[13px] flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        加载中...
      </div>
    );
  }
  if (rules.length === 0) {
    return (
      <div className="text-center py-16">
        <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-primary/10 flex items-center justify-center">
          <Lightbulb className="w-8 h-8 text-primary/50" />
        </div>
        <p className="text-[15px] font-medium text-foreground mb-2">还没有洞察</p>
        <p className="text-[13px] text-muted-foreground mb-1">
          点击上方 <span className="text-primary font-medium">"发现"</span> 按钮扫描数据源，
        </p>
        <p className="text-[13px] text-muted-foreground mb-4">
          系统会自动识别可监控的表，生成洞察建议
        </p>
        <div className="inline-flex items-center gap-2">
          <Link
            to="/insights/rules/new"
            className="px-4 py-2 text-[13px] bg-primary text-white rounded-md hover:bg-primary-hover flex items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            手动新建洞察
          </Link>
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {rules.map((rule, idx) => (
        <Link
          key={rule.id}
          to={`/insights/rules/${rule.id}`}
          className={`animate-slide-up stagger-${idx + 1} block bg-white border border-border-light rounded-lg p-4 hover:border-primary hover:shadow-sm transition-all`}
        >
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[14px] font-semibold truncate">{rule.name}</h3>
            <span
              className={`text-[10px] px-2 py-0.5 rounded flex-shrink-0 ml-2 ${
                rule.enabled
                  ? "bg-emerald-50 text-emerald-600"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              {rule.enabled ? "运行中" : "已暂停"}
            </span>
          </div>
          <p className="text-[12px] text-muted-foreground mb-3 line-clamp-2">
            {rule.description || `监控表 ${rule.queryConfig?.table ?? "—"}`}
          </p>
          <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
            <span>{rule.scheduleTime}</span>
            <span>·</span>
            <span>{rule.reportType === "daily_report" ? "日报" : "周报"}</span>
            {rule.lastRunAt && (
              <>
                <span>·</span>
                <span>上次: {new Date(rule.lastRunAt).toLocaleDateString("zh-CN")}</span>
              </>
            )}
          </div>
        </Link>
      ))}
    </div>
  );
}

// ============ 建议列表 ============
function SuggestionsList({
  suggestions,
  loading,
  onActed,
}: {
  suggestions: InsightSuggestion[];
  loading: boolean;
  onActed: () => void;
}) {
  const [actingId, setActingId] = useState<string | null>(null);

  const handleAccept = async (s: InsightSuggestion) => {
    setActingId(s.id);
    try {
      await acceptSuggestion(s.id);
      onActed();
    } catch (e) {
      alert(e instanceof Error ? e.message : "接受建议失败");
    } finally {
      setActingId(null);
    }
  };

  const handleDismiss = async (s: InsightSuggestion) => {
    setActingId(s.id);
    try {
      await dismissSuggestion(s.id);
      onActed();
    } catch (e) {
      alert(e instanceof Error ? e.message : "忽略建议失败");
    } finally {
      setActingId(null);
    }
  };

  if (loading) {
    return (
      <div className="text-muted-foreground text-[13px] flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        加载中...
      </div>
    );
  }

  const pending = suggestions.filter((s) => s.status === "pending");
  const acted = suggestions.filter((s) => s.status !== "pending");

  if (suggestions.length === 0) {
    return (
      <div className="text-center py-16">
        <Sparkles className="w-12 h-12 mx-auto text-muted-foreground/40 mb-4" />
        <p className="text-[14px] text-muted-foreground mb-1">暂无监控建议</p>
        <p className="text-[12px] text-muted-foreground/70">
          点击上方"发现"按钮扫描数据源，系统会自动识别可监控的表
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {pending.length > 0 && (
        <div>
          <h3 className="text-[12px] font-medium text-muted-foreground uppercase mb-2">
            待处理 ({pending.length})
          </h3>
          <div className="space-y-2">
            {pending.map((s, idx) => (
              <div key={s.id} className={`animate-slide-up stagger-${idx + 1}`}>
                <SuggestionCard
                  suggestion={s}
                  onAccept={() => handleAccept(s)}
                  onDismiss={() => handleDismiss(s)}
                  acting={actingId === s.id}
                />
              </div>
            ))}
          </div>
        </div>
      )}
      {acted.length > 0 && (
        <div>
          <h3 className="text-[12px] font-medium text-muted-foreground uppercase mb-2">
            已处理 ({acted.length})
          </h3>
          <div className="space-y-2">
            {acted.map((s, idx) => (
              <div key={s.id} className={`animate-slide-up stagger-${idx + 1}`}>
                <SuggestionCard
                  suggestion={s}
                  onAccept={() => handleAccept(s)}
                  onDismiss={() => handleDismiss(s)}
                  acting={actingId === s.id}
                />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SuggestionCard({
  suggestion,
  onAccept,
  onDismiss,
  acting,
}: {
  suggestion: InsightSuggestion;
  onAccept: () => void;
  onDismiss: () => void;
  acting: boolean;
}) {
  const confidence = suggestion.confidence ?? 0;
  const confidencePct = Math.round(confidence * 100);
  const isPending = suggestion.status === "pending";

  return (
    <div className="bg-white border border-border-light rounded-lg p-4 hover:shadow-sm transition-shadow">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h4 className="text-[14px] font-medium truncate">
              {suggestion.suggestedName || suggestion.tableName}
            </h4>
            <span
              className={`text-[10px] px-1.5 py-0.5 rounded ${
                isPending
                  ? "bg-warning-light text-warning"
                  : suggestion.status === "accepted"
                    ? "bg-success-light text-success"
                    : "bg-muted text-muted-foreground"
              }`}
            >
              {isPending ? "待处理" : suggestion.status === "accepted" ? "已采纳" : "已忽略"}
            </span>
          </div>
          <p className="text-[12px] text-muted-foreground line-clamp-2">
            {suggestion.rationale || `表 ${suggestion.tableName}`}
          </p>
        </div>
        <div className="text-right flex-shrink-0">
          <div className="text-[11px] text-muted-foreground">置信度</div>
          <div className={`text-[14px] font-semibold ${confidencePct >= 80 ? "text-success" : confidencePct >= 60 ? "text-warning" : "text-muted-foreground"}`}>
            {confidencePct}%
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3 text-[11px] text-muted-foreground mb-3">
        <span>表: {suggestion.tableName}</span>
        {suggestion.timeField && (
          <>
            <span>·</span>
            <span>时间: {suggestion.timeField}</span>
          </>
        )}
        {suggestion.measureFields && suggestion.measureFields.length > 0 && (
          <>
            <span>·</span>
            <span>度量: {suggestion.measureFields.slice(0, 3).join(", ")}</span>
          </>
        )}
        {suggestion.rowCountEstimate && (
          <>
            <span>·</span>
            <span>{suggestion.rowCountEstimate.toLocaleString()} 行</span>
          </>
        )}
      </div>

      {isPending && (
        <div className="flex items-center gap-2">
          <button
            onClick={onAccept}
            disabled={acting}
            className="px-3 py-1 text-[12px] bg-primary text-white rounded hover:bg-primary-hover disabled:opacity-50 flex items-center gap-1"
          >
            {acting ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
            采纳并创建规则
          </button>
          <button
            onClick={onDismiss}
            disabled={acting}
            className="px-3 py-1 text-[12px] border border-border rounded text-muted-foreground hover:bg-muted disabled:opacity-50"
          >
            忽略
          </button>
        </div>
      )}
    </div>
  );
}
