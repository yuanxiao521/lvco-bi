import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  GitBranch,
  Activity,
  History,
  Loader2,
  Database,
  LayoutDashboard,
  Users,
  Workflow,
  ArrowRight,
} from "lucide-react";
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  getMetric,
  getMetricDependencies,
  getMetricDependents,
  getMetricImpact,
  getMetricLineage,
  listMetricVersions,
  compareMetricVersions,
} from "../../api/metrics";
import type {
  MetricDefinition,
  MetricImpact,
  MetricLineageEdge,
  MetricVersionCompare,
  MetricVersionItem,
} from "../../types/metric";

type TabKey = "lineage" | "impact" | "versions";

type MetricNodeData = { label: string; type: string };
type MetricFlowNode = Node<MetricNodeData, "metric">;

function MetricNode({ data }: NodeProps<MetricFlowNode>) {
  const isDerived = data.type === "derived";
  return (
    <div
      className={`px-3 py-2 rounded-lg border shadow-sm text-[12px] ${
        isDerived
          ? "bg-ai-light border-ai text-ai"
          : "bg-primary-light border-primary text-primary"
      }`}
    >
      <div className="font-semibold">{data.label}</div>
    </div>
  );
}

const nodeTypes = { metric: MetricNode } satisfies NodeTypes;

function LineageGraph({
  current,
  dependencies,
  dependents,
}: {
  current: MetricDefinition;
  dependencies: MetricDefinition[];
  dependents: MetricDefinition[];
}) {
  const nodes = useMemo(() => {
    const list: MetricFlowNode[] = [];
    const gap = 40;
    dependencies.forEach((dep, i) => {
      list.push({
        id: dep.id,
        type: "metric",
        position: { x: 0, y: i * gap },
        data: { label: dep.name, type: dep.formulaType },
      });
    });
    list.push({
      id: current.id,
      type: "metric",
      position: { x: 260, y: Math.max(dependencies.length, 1) * gap },
      data: { label: `${current.name}（当前）`, type: current.formulaType },
    });
    dependents.forEach((dept, i) => {
      list.push({
        id: dept.id,
        type: "metric",
        position: { x: 520, y: (dependents.length - i) * gap },
        data: { label: dept.name, type: dept.formulaType },
      });
    });
    return list;
  }, [current, dependencies, dependents]);

  const edges = useMemo(() => {
    const list: Edge[] = [];
    dependencies.forEach((dep) => {
      list.push({ id: `${dep.id}-${current.id}`, source: dep.id, target: current.id, type: "smoothstep" });
    });
    dependents.forEach((dept) => {
      list.push({ id: `${current.id}-${dept.id}`, source: current.id, target: dept.id, type: "smoothstep" });
    });
    return list;
  }, [current, dependencies, dependents]);

  return (
    <div className="h-[420px] border border-border-light rounded-md overflow-hidden bg-[#FAFBFC]">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
      >
        <Background gap={16} />
        <Controls />
      </ReactFlow>
    </div>
  );
}

function FieldLineage({ items }: { items: MetricLineageEdge[] }) {
  if (items.length === 0) {
    return (
      <div className="py-10 text-center text-[13px] text-muted-foreground">
        暂无字段血缘信息
      </div>
    );
  }
  return (
    <div className="space-y-2.5">
      {items.map((e, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3 bg-muted/40 rounded-lg">
          <div className="min-w-0 flex-1">
            <div className="text-[12px] font-medium text-foreground font-mono">{e.source_field}</div>
            <div className="text-[11px] text-muted-foreground">{e.dataset_name || "—"}</div>
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground shrink-0">
            <ArrowRight className="w-3.5 h-3.5" />
            <span className="px-2 py-0.5 rounded bg-white border border-border-light">{e.transform || "直接映射"}</span>
          </div>
          <div className="min-w-0 flex-1 text-right">
            <div className="text-[12px] font-medium text-foreground font-mono">{e.target_field}</div>
            <div className="text-[11px] text-muted-foreground">目标字段</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ImpactCard({ impact }: { impact: MetricImpact | null }) {
  const cards = [
    { icon: Workflow, label: "被引用指标数", value: impact?.dependents_count ?? "—", color: "text-primary", bg: "bg-primary-light" },
    { icon: LayoutDashboard, label: "引用仪表盘数", value: impact?.dashboards_count ?? "—", color: "text-chart-2", bg: "bg-success-light" },
    { icon: Users, label: "影响用户数", value: impact?.users_count ?? "—", color: "text-chart-6", bg: "bg-[#F3F0FF]" },
  ];
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {cards.map((c) => (
        <div key={c.label} className="bg-white rounded-[10px] shadow-card border border-border-light p-4 flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg ${c.bg} flex items-center justify-center`}>
            <c.icon className={`w-5 h-5 ${c.color}`} />
          </div>
          <div>
            <div className="text-[12px] text-muted-foreground">{c.label}</div>
            <div className="text-[18px] font-semibold text-foreground">{c.value}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function VersionPanel({
  metricId,
  currentVersion,
}: {
  metricId: string;
  currentVersion: string;
}) {
  const [versions, setVersions] = useState<MetricVersionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [compare, setCompare] = useState<MetricVersionCompare | null>(null);
  const [comparing, setComparing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listMetricVersions(metricId)
      .then((v) => setVersions(v))
      .catch(() => setVersions([]))
      .finally(() => setLoading(false));
  }, [metricId]);

  const runCompare = async () => {
    if (!from || !to || from === to) return;
    setComparing(true);
    setError(null);
    try {
      const res = await compareMetricVersions(metricId, from, to);
      setCompare(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "版本对比失败");
      setCompare(null);
    } finally {
      setComparing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-[10px] shadow-card border border-border-light p-4">
        <h3 className="text-[13px] font-semibold text-foreground mb-3">
          版本历史（当前：{currentVersion}）
        </h3>
        {loading ? (
          <div className="py-6 text-center text-[12px] text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin mx-auto mb-2" />
            加载版本历史...
          </div>
        ) : versions.length === 0 ? (
          <div className="py-6 text-center text-[13px] text-muted-foreground">
            暂无历史版本，可在列表页「发布版本」创建
          </div>
        ) : (
          <div className="space-y-2">
            {versions.map((v) => (
              <div key={v.version} className="flex items-center gap-3 px-3 py-2 bg-muted/40 rounded-lg">
                <span className="px-2 py-0.5 rounded bg-primary-light text-primary text-[11px] font-medium font-mono">
                  {v.version}
                </span>
                <span className="text-[12px] text-muted-foreground flex-1">
                  {v.change_note || "（无变更说明）"}
                </span>
                <span className="text-[11px] text-muted-foreground shrink-0">{v.created_at || "—"}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="bg-white rounded-[10px] shadow-card border border-border-light p-4">
        <h3 className="text-[13px] font-semibold text-foreground mb-3">版本对比</h3>
        <div className="flex flex-wrap items-end gap-3 mb-4">
          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">从版本</label>
            <select
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
            >
              <option value="">选择版本</option>
              {versions.map((v) => (
                <option key={v.version} value={v.version}>{v.version}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">到版本</label>
            <select
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
            >
              <option value="">选择版本</option>
              {versions.map((v) => (
                <option key={v.version} value={v.version}>{v.version}</option>
              ))}
            </select>
          </div>
          <button
            onClick={runCompare}
            disabled={comparing || !from || !to || from === to}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 transition-colors"
          >
            {comparing && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            对比
          </button>
        </div>

        {error && <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px] mb-3">{error}</div>}

        {compare && (
          compare.changes.length === 0 ? (
            <div className="py-6 text-center text-[13px] text-muted-foreground">
              {compare.from_version} 与 {compare.to_version} 无差异
            </div>
          ) : (
            <div className="overflow-hidden border border-border-light rounded-md">
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="bg-muted">
                    <th className="text-left px-3 py-2 font-medium text-muted-foreground">字段</th>
                    <th className="text-left px-3 py-2 font-medium text-muted-foreground">{compare.from_version}</th>
                    <th className="text-left px-3 py-2 font-medium text-muted-foreground">{compare.to_version}</th>
                  </tr>
                </thead>
                <tbody>
                  {compare.changes.map((c, i) => {
                    const fromStr = typeof c.from === "object" ? JSON.stringify(c.from) : String(c.from ?? "null");
                    const toStr = typeof c.to === "object" ? JSON.stringify(c.to) : String(c.to ?? "null");
                    return (
                      <tr key={i} className="border-t border-border-light">
                        <td className="px-3 py-2 font-medium text-foreground">{c.field}</td>
                        <td className="px-3 py-2 text-muted-foreground font-mono line-through">{fromStr}</td>
                        <td className="px-3 py-2 text-danger font-mono">{toStr}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>
    </div>
  );
}

export default function MetricDetail() {
  const { id } = useParams<{ id: string }>();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const tabParam = params.get("tab") as TabKey | null;
  const [tab, setTab] = useState<TabKey>(tabParam === "impact" ? "impact" : tabParam === "versions" ? "versions" : "lineage");

  const [metric, setMetric] = useState<MetricDefinition | null>(null);
  const [dependencies, setDependencies] = useState<MetricDefinition[]>([]);
  const [dependents, setDependents] = useState<MetricDefinition[]>([]);
  const [impact, setImpact] = useState<MetricImpact | null>(null);
  const [lineage, setLineage] = useState<MetricLineageEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError(null);
    Promise.all([getMetric(id), getMetricDependencies(id), getMetricDependents(id)])
      .then(([m, deps, depts]) => {
        setMetric(m);
        setDependencies(deps);
        setDependents(depts);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "加载指标失败"))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (!id || tab !== "impact") return;
    getMetricImpact(id)
      .then(setImpact)
      .catch(() => setImpact(null));
  }, [id, tab]);

  useEffect(() => {
    if (!id || tab !== "lineage") return;
    getMetricLineage(id)
      .then(setLineage)
      .catch(() => setLineage([]));
  }, [id, tab]);

  const selectTab = (t: TabKey) => {
    setTab(t);
    setParams(t === "lineage" ? {} : { tab: t });
  };

  const tabDefs: Array<{ key: TabKey; label: string; icon: typeof GitBranch }> = [
    { key: "lineage", label: "血缘依赖", icon: GitBranch },
    { key: "impact", label: "影响分析", icon: Activity },
    { key: "versions", label: "版本治理", icon: History },
  ];

  if (loading) {
    return (
      <div className="flex-1 p-6">
        <div className="py-20 text-center text-[13px] text-muted-foreground">
          <span className="inline-flex items-center gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            正在加载指标...
          </span>
        </div>
      </div>
    );
  }

  if (error || !metric) {
    return (
      <div className="flex-1 p-6">
        <button onClick={() => navigate("/metrics")} className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-primary mb-4">
          <ArrowLeft className="w-4 h-4" />返回指标列表
        </button>
        <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">{error || "指标不存在"}</div>
      </div>
    );
  }

  return (
    <div className="flex-1 p-6 space-y-5">
      <button
        onClick={() => navigate("/metrics")}
        className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-primary transition-colors"
      >
        <ArrowLeft className="w-4 h-4" />返回指标列表
      </button>

      <header className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
          <Database className="w-5 h-5 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="text-[17px] font-semibold text-foreground truncate">{metric.name}</h1>
            <span className={metric.formulaType === "derived" ? "inline-flex px-2 py-0.5 rounded text-[11px] font-medium bg-ai-light text-ai" : "inline-flex px-2 py-0.5 rounded text-[11px] font-medium bg-primary-light text-primary"}>
              {metric.formulaType === "derived" ? "派生" : "基础"}
            </span>
            <span className="px-2 py-0.5 rounded bg-muted text-muted-foreground text-[11px] font-mono">{metric.version}</span>
          </div>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            <span className="font-mono">{metric.key}</span> · {metric.formula}
          </p>
        </div>
      </header>

      <div className="flex gap-0 border-b border-border-light">
        {tabDefs.map((t) => (
          <button
            key={t.key}
            onClick={() => selectTab(t.key)}
            className={`px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors flex items-center gap-1.5 ${
              tab === t.key
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <t.icon className="w-3.5 h-3.5" />
            {t.label}
          </button>
        ))}
      </div>

      {tab === "lineage" && (
        <div className="space-y-4">
          <div className="bg-white rounded-[10px] shadow-card border border-border-light p-4">
            <h3 className="text-[13px] font-semibold text-foreground mb-3">
              指标依赖图（{dependencies.length} 个上游 · {dependents.length} 个下游）
            </h3>
            <LineageGraph current={metric} dependencies={dependencies} dependents={dependents} />
          </div>
          <div className="bg-white rounded-[10px] shadow-card border border-border-light p-4">
            <h3 className="text-[13px] font-semibold text-foreground mb-3">字段血缘</h3>
            <FieldLineage items={lineage} />
          </div>
        </div>
      )}

      {tab === "impact" && (
        <div className="space-y-4">
          <ImpactCard impact={impact} />
          <p className="text-[12px] text-muted-foreground">
            影响分析展示该指标被下游指标、仪表盘与用户引用的规模，用于变更前评估风险。
          </p>
        </div>
      )}

      {tab === "versions" && <VersionPanel metricId={metric.id} currentVersion={metric.version} />}
    </div>
  );
}