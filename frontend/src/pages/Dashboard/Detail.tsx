import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import {
  ArrowLeft,
  RefreshCw,
  Share2,
  Maximize2,
  X,
  Copy,
  Check,
  Loader2,
  Users,
  TrendingUp,
  DollarSign,
  ShoppingCart,
  BarChart2,
  Activity,
  Target,
} from "lucide-react";
import { useQuery } from "../../hooks/useQuery";
import {
  getDashboard,
  getDashboardData,
  shareDashboard,
  refreshDashboard,
} from "../../api/dashboards";
import type {
  DashboardDataResult,
  DashboardDetail,
  DashboardChartEntry,
  DashboardChartItem,
  DashboardChartErrorItem,
} from "../../types/dashboard";
import ChartRenderer from "../../components/charts/ChartRenderer";
import { useInView } from "../../hooks/useInView";
import { useAuthStore } from "../../stores/authStore";

function formatRelative(input: string | null | undefined): string {
  if (!input) return "未知";
  const target = new Date(input);
  if (Number.isNaN(target.getTime())) return "未知";
  const diffMs = Date.now() - target.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "刚刚";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}小时前`;
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 30) return `${diffDay}天前`;
  const diffMonth = Math.floor(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth}个月前`;
  const diffYear = Math.floor(diffDay / 365);
  return `${diffYear}年前`;
}

function isChartItem(
  entry: DashboardChartEntry,
): entry is DashboardChartItem {
  return (entry as DashboardChartItem).data !== undefined;
}

function isChartError(
  entry: DashboardChartEntry,
): entry is DashboardChartErrorItem {
  return (entry as DashboardChartErrorItem).error !== undefined;
}

function ChartCardSkeleton() {
  return (
    <div className="bg-card rounded-md shadow-card p-5">
      <div className="h-4 w-32 bg-muted rounded mb-5 animate-pulse" />
      <div className="h-[220px] flex items-center justify-center bg-muted/40 rounded animate-pulse">
        <Loader2 className="w-5 h-5 text-muted-foreground animate-spin" />
      </div>
    </div>
  );
}

function LazyDashboardChart({ children }: { children: React.ReactNode }) {
  const { ref, inView } = useInView()
  return (
    <div ref={ref} style={{ minHeight: 300 }}>
      {inView ? children : (
        <div className="animate-pulse bg-muted rounded-lg" style={{ height: 300 }} />
      )}
    </div>
  )
}

function ChartCard({
  entry,
  height = 260,
}: {
  entry: DashboardChartEntry;
  height?: number;
}) {
  if (isChartError(entry)) {
    return (
      <div className="bg-card rounded-md shadow-card p-5">
        <h3 className="text-[14px] font-semibold text-foreground mb-4">
          {entry.title ?? "未命名图表"}
        </h3>
        <div className="flex items-center justify-center text-[12px] text-danger" style={{ height }}>
          加载失败：{entry.error}
        </div>
      </div>
    );
  }
  if (!isChartItem(entry)) {
    return null;
  }
  const config = {
    dimensions: entry.dimensions ?? entry.data.columns.slice(0, 1),
    measures: entry.measures ?? [],
    chartType: entry.chartType,
  } as Parameters<typeof ChartRenderer>[0]["config"];
  const renderer = (entry.renderConfig?.renderer || "echarts") as "recharts" | "echarts";
  const palette = entry.renderConfig?.palette;
  return (
    <div className="bg-card rounded-md shadow-card p-5">
      <h3 className="text-[14px] font-semibold text-foreground mb-4">
        {entry.title ?? "未命名图表"}
      </h3>
      <div style={{ width: "100%", height }}>
        <ChartRenderer config={config} result={entry.data} renderer={renderer} palette={palette} />
      </div>
    </div>
  );
}

function ModalShell({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden
      />
      <div className="relative bg-white rounded-lg shadow-xl w-[min(480px,92vw)] max-h-[88vh] flex flex-col">
        <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
          <h3 className="text-[15px] font-semibold text-foreground">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-5 py-4 overflow-auto flex-1">{children}</div>
        {footer ? (
          <div className="px-5 py-3 border-t border-border-light flex items-center justify-end gap-2">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function DashboardDetail() {
  const { id } = useParams<{ id: string }>();
  const currentUser = useAuthStore((s) => s.user);
  const ownerFallback = currentUser?.displayName ?? "我";

  const {
    data: dashboard,
    loading: dashLoading,
    error: dashError,
  } = useQuery<DashboardDetail>(
    () => getDashboard(id as string),
    [id],
  );

  const {
    data: dashboardData,
    loading: dataLoading,
    error: dataError,
  } = useQuery<DashboardDataResult>(
    () => getDashboardData(id as string),
    [id],
  );

  const [shareOpen, setShareOpen] = useState(false);
  const [shareUrl, setShareUrl] = useState<string>("");
  const [shareError, setShareError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // KPI 配色盘（与图表配色一致）
  const KPI_COLORS = ["#2BB5A0", "#6C7BF2", "#F5A623", "#EF5B5B", "#4EADFF", "#A78BFA"];

  // 根据字段名猜测图标组件
  const getIconForField = (field: string) => {
    const f = field.toLowerCase();
    if (/fans|粉丝|关注|用户|人数|member|customer|user/.test(f)) return Users;
    if (/sale|销售|收入|gmv|金额|money|revenue|profit/.test(f)) return DollarSign;
    if (/order|订单|成交|transaction/.test(f)) return ShoppingCart;
    if (/rate|rate|rate|转化|click|ctr|转化率/.test(f)) return Target;
    if (/avg|mean|人均|平均/.test(f)) return TrendingUp;
    return BarChart2;
  };

  // 从图表数据中聚合 KPI 指标卡
  const kpiCards = useMemo(() => {
    const charts = dashboardData?.charts ?? [];
    const cards: Array<{
      label: string;
      value: string;
      rawValue: number;
      agg: string;
      field: string;
      sub: string;
      color: string;
      Icon: React.ComponentType<{ className?: string }>;
    }> = [];

    const aggNames: Record<string, string> = {
      SUM: "总计", AVG: "均值", COUNT: "计数", MAX: "最大值", MIN: "最小值",
      COUNT_DISTINCT: "去重", MEDIAN: "中位数", STDDEV: "标准差",
    };

    let colorIdx = 0;

    for (const entry of charts) {
      if (!isChartItem(entry)) continue;
      const { data, measures, dimensions, title } = entry;
      if (!data?.rows?.length || !measures?.length) continue;

      const isGenericTitle = !title
        || /\b(bar|line|pie|donut|area|scatter|grouped_bar|stacked_bar|horizontal_bar|funnel|heatmap|radar|sankey|kpi_card)\s*(图表)?\b/i.test(title)
        || title === "分析画布";

      for (const measure of measures) {
        const field = measure.field;
        const agg = measure.agg || "SUM";
        const aggLabel = aggNames[agg] || agg;
        const values = data.rows
          .map((r: Record<string, unknown>) => Number(r[field]))
          .filter((v: number) => !isNaN(v));
        if (!values.length) continue;

        let computed: number;
        switch (agg) {
          case "AVG": computed = values.reduce((a, b) => a + b, 0) / values.length; break;
          case "MAX": computed = Math.max(...values); break;
          case "MIN": computed = Math.min(...values); break;
          case "COUNT":
          case "COUNT_DISTINCT": computed = values.length; break;
          default: computed = values.reduce((a, b) => a + b, 0); break;
        }

        let displayValue: string;
        if (computed >= 1e8) displayValue = (computed / 1e8).toFixed(1) + "亿";
        else if (computed >= 1e4) displayValue = (computed / 1e4).toFixed(1) + "万";
        else if (computed >= 1000) displayValue = computed.toLocaleString();
        else displayValue = computed.toFixed(computed % 1 === 0 ? 0 : 1);

        let dimContext = "";
        if (dimensions?.length) {
          const dimLabels = dimensions.map(d => {
            const val = data.rows[0]?.[d];
            return val != null ? String(val) : "";
          }).filter(Boolean);
          if (dimLabels.length) dimContext = dimLabels.join(" · ");
        }

        const cardLabel = isGenericTitle
          ? `${field}`
          : (title ?? field);

        const subParts: string[] = [];
        if (isGenericTitle) subParts.push(aggLabel);
        if (dimContext) subParts.push(dimContext);

        const color = KPI_COLORS[colorIdx % KPI_COLORS.length];
        colorIdx++;

        cards.push({
          label: cardLabel,
          value: displayValue,
          rawValue: computed,
          agg: aggLabel,
          field,
          sub: subParts.join(" · "),
          color,
          Icon: getIconForField(field),
        });
      }
    }
    return cards.slice(0, 6);
  }, [dashboardData]);

  const handleRefresh = async () => {
    if (!id || refreshing) return;
    setRefreshing(true);
    try {
      await refreshDashboard(id);
      // 重新获取数据（触发 useQuery 重新执行需要改变依赖，直接重新加载）
      window.location.reload();
    } catch {
      setRefreshing(false);
    }
  };

  const handleShare = useCallback(async () => {
    if (!id) return;
    setSharing(true);
    setShareError(null);
    try {
      const result = await shareDashboard(id);
      const fullUrl = result.shareUrl.startsWith("http")
        ? result.shareUrl
        : `${window.location.origin}${result.shareUrl}`;
      setShareUrl(fullUrl);
      setShareOpen(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "生成分享链接失败";
      setShareError(msg);
      setShareUrl(window.location.href);
      setShareOpen(true);
    } finally {
      setSharing(false);
    }
  }, [id]);

  const handleCopy = useCallback(async () => {
    if (!shareUrl) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(shareUrl);
      } else {
        const ta = document.createElement("textarea");
        ta.value = shareUrl;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }, [shareUrl]);

  const handleFullscreen = useCallback(() => {
    const el = document.documentElement;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else if (el.requestFullscreen) {
      void el.requestFullscreen();
    }
  }, []);

  useEffect(() => {
    return () => {
      setShareOpen(false);
      setShareUrl("");
      setShareError(null);
      setCopied(false);
    };
  }, [id]);

  if (!id) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-[13px] text-muted-foreground">缺少仪表盘 ID</p>
      </div>
    );
  }

  const charts = dashboardData?.charts ?? [];

  return (
    <div className="flex-1 overflow-auto">
      <header className="h-14 flex items-center justify-between px-6 border-b border-border bg-white flex-shrink-0">
        <div className="flex items-center gap-3">
          <Link
            to="/dashboard"
            className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-muted transition-colors"
          >
            <ArrowLeft className="w-4 h-4 text-muted-foreground" />
          </Link>
          <div>
            <h1 className="text-[15px] font-semibold text-foreground leading-tight">
              {dashLoading
                ? "加载中..."
                : dashError
                ? "加载失败"
                : dashboard?.title ?? "未命名仪表盘"}
            </h1>
            <p className="text-[12px] text-muted-foreground">
              {dashboard
                ? `更新于 ${formatRelative(dashboard.updatedAt)} · 由 ${
                    dashboard.ownerName ?? ownerFallback
                  } 创建`
                : "\u00A0"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing}
            className="h-8 px-3 rounded-lg border border-border bg-white text-[13px] text-card-foreground hover:bg-muted transition-colors flex items-center gap-1.5 disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
          <button
            type="button"
            onClick={handleShare}
            disabled={sharing}
            className="h-8 px-3 rounded-lg border border-border bg-white text-[13px] text-card-foreground hover:bg-muted transition-colors flex items-center gap-1.5 disabled:opacity-50"
          >
            {sharing ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Share2 className="w-3.5 h-3.5" />
            )}
            <span>分享</span>
          </button>
          <button
            type="button"
            onClick={handleFullscreen}
            className="h-8 px-3 rounded-lg border border-border bg-white text-[13px] text-card-foreground hover:bg-muted transition-colors flex items-center gap-1.5"
          >
            <Maximize2 className="w-3.5 h-3.5" />
            <span>全屏</span>
          </button>
        </div>
      </header>

      <div className="p-6">
        {dashError ? (
          <div className="mb-6 p-4 rounded-lg border border-danger/30 bg-danger/5 text-[13px] text-danger">
            加载仪表盘失败：{dashError.message}
          </div>
        ) : null}
        {dataError ? (
          <div className="mb-6 p-4 rounded-lg border border-danger/30 bg-danger/5 text-[13px] text-danger">
            加载图表数据失败：{dataError.message}
          </div>
        ) : null}

        {dataLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <ChartCardSkeleton />
            <ChartCardSkeleton />
            <ChartCardSkeleton />
            <ChartCardSkeleton />
          </div>
        ) : charts.length === 0 ? (
          <div className="bg-card rounded-md shadow-card p-12 flex flex-col items-center justify-center text-center">
            <h3 className="text-[16px] font-semibold text-foreground mb-2">
              暂无图表
            </h3>
            <p className="text-[12px] text-muted-foreground max-w-sm">
              该仪表盘还没有添加图表。可以前往画布或图表配置页添加图表到当前仪表盘。
            </p>
          </div>
        ) : (
          <>
            {/* KPI 指标卡汇总 */}
            {kpiCards.length > 0 ? (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-3 xl:grid-cols-4 gap-4 mb-6">
                {kpiCards.map((kpi, idx) => {
                  const Icon = kpi.Icon;
                  return (
                    <div
                      key={idx}
                      className="relative bg-white rounded-xl p-5 shadow-card border border-border-light hover:shadow-float transition-all hover:-translate-y-0.5 group overflow-hidden"
                      style={{ borderLeftColor: kpi.color, borderLeftWidth: '3px' }}
                    >
                      {/* 背景装饰圆形 */}
                      <div
                        className="absolute -top-6 -right-6 w-20 h-20 rounded-full opacity-10 group-hover:opacity-20 transition-opacity"
                        style={{ backgroundColor: kpi.color }}
                      />
                      <div className="flex items-start justify-between mb-3">
                        <div
                          className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
                          style={{ backgroundColor: kpi.color + '18' }}
                        >
                          <Icon className="w-4.5 h-4.5" style={{ color: kpi.color }} />
                        </div>
                        <span
                          className="text-[10px] font-medium px-2 py-0.5 rounded-full"
                          style={{ backgroundColor: kpi.color + '15', color: kpi.color }}
                        >
                          {kpi.agg}
                        </span>
                      </div>
                      <div className="mb-1">
                        <p className="text-[12px] font-medium text-foreground truncate" title={kpi.label}>
                          {kpi.label}
                        </p>
                      </div>
                      <p
                        className="text-[28px] font-bold tracking-tight leading-none mb-2"
                        style={{ color: kpi.color }}
                      >
                        {kpi.value}
                      </p>
                      {kpi.sub ? (
                        <p className="text-[11px] text-muted-foreground truncate" title={kpi.sub}>
                          {kpi.sub}
                        </p>
                      ) : (
                        <p className="text-[11px] text-muted-foreground">
                          {kpi.field}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : null}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {charts.map((entry) => (
              <LazyDashboardChart key={entry.chartId}>
                <ChartCard entry={entry} />
              </LazyDashboardChart>
            ))}
          </div>
          </>
        )}
      </div>

      <ModalShell
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        title="分享仪表盘"
        footer={
          <button
            type="button"
            onClick={() => setShareOpen(false)}
            className="h-8 px-3 rounded-lg border border-border bg-white text-[13px] text-card-foreground hover:bg-muted transition-colors"
          >
            关闭
          </button>
        }
      >
        <p className="text-[13px] text-card-foreground mb-3">
          将以下链接发送给其他人即可访问该仪表盘：
        </p>
        <div className="flex items-stretch gap-2">
          <input
            type="text"
            readOnly
            value={shareUrl}
            className="flex-1 h-9 px-3 rounded-lg border border-border bg-muted/30 text-[12px] text-foreground font-mono"
            onFocus={(e) => e.currentTarget.select()}
          />
          <button
            type="button"
            onClick={handleCopy}
            className="h-9 px-3 rounded-lg bg-primary text-white text-[13px] hover:bg-primary-hover transition-colors flex items-center gap-1.5"
          >
            {copied ? (
              <>
                <Check className="w-3.5 h-3.5" />
                已复制
              </>
            ) : (
              <>
                <Copy className="w-3.5 h-3.5" />
                复制
              </>
            )}
          </button>
        </div>
        {shareError ? (
          <p className="mt-3 text-[12px] text-warning">
            服务端分享链接生成失败（{shareError}），已显示当前页面 URL 作为回退。
          </p>
        ) : null}
      </ModalShell>
    </div>
  );
}