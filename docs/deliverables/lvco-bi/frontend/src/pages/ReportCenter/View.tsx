import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Loader2, Clock, User } from "lucide-react";
import { getReport } from "../../api/reports";
import ChartRenderer from "../../components/charts/ChartRenderer";
import type { ReportDetail, ChartQueryConfig, QueryResult } from "../../api/types";
import { useAuthStore } from "../../stores/authStore";

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  draft: { label: "草稿", className: "bg-warning-light text-warning" },
  published: { label: "已发布", className: "bg-success-light text-success" },
  shared: { label: "已分享", className: "bg-ai-light text-ai" },
  archived: { label: "已归档", className: "bg-muted text-muted-foreground" },
};

const SOURCE_TYPE_BADGE: Record<string, { label: string; className: string }> = {
  canvas: { label: "画布", className: "bg-primary-light text-primary" },
  dashboard: { label: "仪表盘", className: "bg-ai-light text-ai" },
  manual: { label: "手动", className: "bg-muted text-muted-foreground" },
  ai_insight: { label: "AI日报", className: "bg-warning-light text-warning" },
};

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  if (diff < 0) return new Date(iso).toLocaleDateString("zh-CN");
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

export default function ReportView() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const authUser = useAuthStore((s) => s.user);

  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError(null);
    getReport(id)
      .then((d) => setDetail(d))
      .catch((e) => setError(e instanceof Error ? e.message : "加载报表失败"))
      .finally(() => setLoading(false));
  }, [id]);

  const blocks = detail?.snapshotBlocks?.blocks ?? [];
  const statusBadge = detail ? STATUS_BADGE[detail.status] : null;
  const sourceBadge = detail ? SOURCE_TYPE_BADGE[detail.sourceType] : null;

  const ownerFallback = authUser?.displayName ?? "我";

  // 从 chart block 中提取 _chartConfig 和 _chartResult
  const getChartConfig = (block: Record<string, unknown>): ChartQueryConfig | null => {
    if (block._chartConfig) return block._chartConfig as ChartQueryConfig;
    return null;
  };

  const getChartResult = (block: Record<string, unknown>): QueryResult | null => {
    if (block._chartResult) return block._chartResult as QueryResult;
    return null;
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        <span className="text-[13px] text-muted-foreground">加载中...</span>
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3">
        <p className="text-[13px] text-muted-foreground">{error || "报表不存在"}</p>
        <button
          onClick={() => navigate("/report-center")}
          className="px-4 py-2 rounded-[6px] text-[13px] font-medium text-white bg-primary"
        >
          返回报表中心
        </button>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0">
      {/* 页头 */}
      <header className="h-16 bg-white border-b border-border flex items-center px-6 flex-shrink-0 gap-4">
        <button
          onClick={() => navigate("/report-center")}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-[6px] text-[13px] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          返回
        </button>
        <div className="flex-1 flex items-center gap-3 min-w-0">
          <h1 className="text-[17px] font-semibold text-foreground truncate">
            {detail.title}
          </h1>
          {sourceBadge && (
            <span className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium flex-shrink-0 ${sourceBadge.className}`}>
              {sourceBadge.label}
            </span>
          )}
          {statusBadge && (
            <span className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium flex-shrink-0 ${statusBadge.className}`}>
              {statusBadge.label}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[12px] text-muted-foreground flex-shrink-0">
          <span className="flex items-center gap-1">
            <User className="w-3 h-3" />
            {ownerFallback}
          </span>
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            {formatRelative(detail.updatedAt)}
          </span>
        </div>
      </header>

      {/* 内容区 */}
      <div className="flex-1 overflow-auto p-8">
        <div className="max-w-[900px] mx-auto space-y-5">
          {blocks.length === 0 ? (
            <p className="text-center py-12 text-[13px] text-muted-foreground">
              报表内容为空
            </p>
          ) : (
            blocks.map((block, i) => {
              const b = block as Record<string, unknown>;

              if (b.type === "h1") {
                return (
                  <h1 key={i} className="text-[24px] font-bold text-foreground border-b-2 border-primary pb-2">
                    {String(b.content ?? "")}
                  </h1>
                );
              }

              if (b.type === "h2") {
                return (
                  <h2 key={i} className="text-[19px] font-semibold text-foreground mt-6">
                    {String(b.content ?? "")}
                  </h2>
                );
              }

              if (b.type === "text") {
                return (
                  <p key={i} className="text-[14px] leading-relaxed text-card-foreground whitespace-pre-wrap">
                    {String(b.content ?? "")}
                  </p>
                );
              }

              if (b.type === "divider") {
                return <hr key={i} className="border-t border-border-light my-4" />;
              }

              if (b.type === "image") {
                const src = b.src as string;
                if (!src) return null;
                return (
                  <div key={i} className="border rounded-lg overflow-hidden bg-muted/30">
                    <img
                      src={src}
                      alt={(b.alt as string) || ""}
                      className="max-w-full max-h-[500px] object-contain mx-auto"
                    />
                  </div>
                );
              }

              if (b.type === "chart") {
                const config = getChartConfig(b);
                const result = getChartResult(b);
                const renderer = (b.renderer as "recharts" | "echarts") || "echarts";
                const palette = (b.palette as string) || undefined;
                const title = (b.title as string) || "图表";

                return (
                  <div
                    key={i}
                    className="bg-white rounded-[10px] border border-border-light p-5 shadow-sm"
                  >
                    <div className="flex items-center gap-2 mb-3">
                      <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-muted text-muted-foreground">
                        图表
                      </span>
                      <span className="text-[13px] font-semibold text-foreground">
                        {title}
                      </span>
                    </div>
                    <div style={{ height: 360 }}>
                      {config && result ? (
                        <ChartRenderer
                          config={config}
                          result={result}
                          renderer={renderer}
                          palette={palette}
                        />
                      ) : (
                        <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
                          图表数据不可用
                        </div>
                      )}
                    </div>
                  </div>
                );
              }

              return null;
            })
          )}
        </div>
      </div>
    </div>
  );
}
