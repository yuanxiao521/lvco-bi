import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Pencil,
  Pin,
  FileOutput,
  Download,
  X,
  Loader2,
  CheckCircle2,
  History,
  Check,
  Trash2,
} from "lucide-react";
import FieldPanel from "../../components/blocks/FieldPanel";
import CanvasBlocks from "../../components/blocks/CanvasBlocks";
import ConfigPanel from "../../components/blocks/ConfigPanel";
import AIAssistant from "./components/AIAssistant";
import {
  executeChartQuery,
  createCanvas,
  updateCanvasBlocks,
  updateCanvas,
  pinCanvasToDashboard,
  saveCanvasAsReport,
  exportCanvasPdf,
  getCanvas,
  listCanvases,
  deleteCanvas,
} from "../../api/canvases";
import { listDashboards, createDashboard } from "../../api/dashboards";
import { getDatasource, listDatasources } from "../../api/datasources";
import type {
  CanvasBlock,
  ChartBlock,
  ChartQueryConfig,
  ChartType,
  FilterConfig,
  MeasureConfig,
  QueryResult,
} from "../../api/types";
import type { DashboardSummary } from "../../api/types";
import type { ReportStatus } from "../../api/types";
import { useToast } from "../../components/ui/Toast";
import { findDefaultTemplate, isSystemTemplateId } from "../../data/defaultTemplates";

const DEFAULT_BLOCKS: CanvasBlock[] = [
  { type: "h1", content: "2024年Q3销售分析报告" },
  {
    type: "text",
    content:
      "本报告基于销售主数据库2024年7月-9月的订单数据，分析各地区销售表现、产品类别分布及客户增长趋势。",
  },
  { type: "divider" },
  { type: "h2", content: "一、各地区销售表现" },
];

const CANVAS_DRAFT_KEY = "lvco:canvas:draft:v1";
const HIDDEN_CANVAS_KEY = "lvco:canvas:hidden:v1";

function getHiddenCanvasIds(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_CANVAS_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function hideCanvasId(id: string) {
  const set = getHiddenCanvasIds();
  set.add(id);
  localStorage.setItem(HIDDEN_CANVAS_KEY, JSON.stringify([...set]));
}

interface CanvasDraft {
  blocks: CanvasBlock[];
  chartConfigs: Record<string, ChartQueryConfig>;
  chartResults: Record<string, QueryResult>;
  selectedDatasourceId: string | null;
  chartType: ChartType;
  dimensions: string[];
  measures: MeasureConfig[];
  filters: FilterConfig[];
  renderer: string;
  canvasId: string | null;
}

function readDraft(): Partial<CanvasDraft> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(CANVAS_DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return parsed as CanvasDraft;
  } catch {
    return null;
  }
}

function writeDraft(draft: CanvasDraft): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(CANVAS_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // QuotaExceeded 或隐私模式：静默降级，不阻塞 UI
  }
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
      <div className="relative bg-white rounded-lg shadow-xl w-[min(520px,92vw)] max-h-[88vh] flex flex-col">
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

export default function FreeCanvas() {
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const templateLoadedRef = useRef(false);
  const [canvasId, setCanvasId] = useState<string | null>(null);
  const [blocks, setBlocks] = useState<CanvasBlock[]>(DEFAULT_BLOCKS);
  const [chartConfigs, setChartConfigs] = useState<Record<string, ChartQueryConfig>>({});
  const [chartResults, setChartResults] = useState<Record<string, QueryResult>>({});
  const [applying, setApplying] = useState(false);

  const [selectedDatasourceId, setSelectedDatasourceId] = useState<string | null>(null);
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [dimensions, setDimensions] = useState<string[]>([]);
  const [measures, setMeasures] = useState<MeasureConfig[]>([]);
  const [filters, setFilters] = useState<FilterConfig[]>([]);
  const [renderer, setRenderer] = useState<string>("echarts");
  const [fieldMeta, setFieldMeta] = useState<Array<{ name: string; data_type: string }> | null>(null);
  const [datasourceList, setDatasourceList] = useState<Array<{id: string; name: string; fields?: Array<{name: string; data_type: string}>}>>([]);

  const [showDashboardModal, setShowDashboardModal] = useState(false);
  const [dashboardList, setDashboardList] = useState<DashboardSummary[]>([]);
  const [dashboardListLoading, setDashboardListLoading] = useState(false);
  const [dashboardListError, setDashboardListError] = useState<string | null>(null);
  const [selectedDashboardId, setSelectedDashboardId] = useState<string | null>(null);
  const [creatingDashboard, setCreatingDashboard] = useState(false);
  const [newDashboardTitle, setNewDashboardTitle] = useState("");
  const [dashboardSaving, setDashboardSaving] = useState(false);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const [dashboardSuccess, setDashboardSuccess] = useState<string | null>(null);

  const [showReportModal, setShowReportModal] = useState(false);
  const [reportTitle, setReportTitle] = useState("");
  const [reportStatus, setReportStatus] = useState<ReportStatus>("draft");
  const [reportSaving, setReportSaving] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportSuccess, setReportSuccess] = useState<string | null>(null);
  const [exportingPdf, setExportingPdf] = useState(false);
  const [canvasTitle, setCanvasTitle] = useState("分析画布");
  const [editingTitle, setEditingTitle] = useState(false);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const [recentCanvases, setRecentCanvases] = useState<Array<{ id: string; title: string; updatedAt: string | null }>>([]);
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(getHiddenCanvasIds);
  const [showRecentCanvases, setShowRecentCanvases] = useState(false);

  const [showFields, setShowFields] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [fieldsCollapsed, setFieldsCollapsed] = useState(false);
  const [configCollapsed, setConfigCollapsed] = useState(false);
  const [recentlyClickedBtn, setRecentlyClickedBtn] = useState<string | null>(null);
  const [selectedBlockIdx, setSelectedBlockIdx] = useState<number | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const lastBackendSyncRef = useRef<string>("");

  // 在 useState lazy initializer 中消费 transfer 数据（首次渲染前只执行一次），
  // 避免 StrictMode 下 effect 双重执行导致 transfer 数据被 remove 后旧草稿覆盖报表 blocks
  const [transferData] = useState<{ blocks: CanvasBlock[]; reportId: string; title: string; datasourceId: string | null } | null>(() => {
    try {
      const raw = localStorage.getItem("lvco:report:transfer");
      if (!raw) return null;
      const data = JSON.parse(raw);
      localStorage.removeItem("lvco:report:transfer");
      if (Array.isArray(data.blocks) && data.blocks.length > 0) return data;
      return null;
    } catch {
      localStorage.removeItem("lvco:report:transfer");
      return null;
    }
  });

  // 挂载时恢复画布数据
  useEffect(() => {
    // 1. 模板加载（优先级最高，template=system-xxx 或 uuid）
    const templateId = searchParams.get("template");
    if (templateId && !templateLoadedRef.current) {
      templateLoadedRef.current = true;
      // 清掉 URL 参数（避免刷新重复加载）
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("template");
      setSearchParams(nextParams, { replace: true });
      // 清掉旧草稿，避免污染
      try {
        localStorage.removeItem(CANVAS_DRAFT_KEY);
      } catch {}

      if (isSystemTemplateId(templateId)) {
        const tpl = findDefaultTemplate(templateId);
        if (tpl) {
          // 深拷贝 block，避免模板定义被改
          const blocksCopy = JSON.parse(JSON.stringify(tpl.blocks)) as CanvasBlock[];
          // 清除 chart block 上残留的 query 结果（无数据源，没有意义）
          const cleaned = blocksCopy.map((b) => {
            if (b.type === "chart") {
              const raw = b as Record<string, unknown>;
              const { _chartConfig, _chartResult, ...rest } = raw;
              return rest as CanvasBlock;
            }
            return b;
          });
          setBlocks(cleaned);
          setChartConfigs({});
          setChartResults({});
          setSelectedDatasourceId(null);
          setCanvasId(null);
          setDimensions([]);
          setMeasures([]);
          setFilters([]);
          setChartType("bar");
          setRenderer("echarts");
          setHydrated(true);
          toast.success(`已加载「${tpl.title}」模板，请选择数据源后生成图表`);
          return;
        }
      } else {
        // 用户画布 UUID：克隆其 blocks + 数据源
        (async () => {
          try {
            const source = await getCanvas(templateId);
            const sourceBlocks: CanvasBlock[] = Array.isArray(source.blocks)
              ? (source.blocks as CanvasBlock[])
              : [];
            // 深拷贝 + 清掉 chart block 上的 _chartConfig/_chartResult（旧数据的查询结果不应复制）
            const cleaned = sourceBlocks.map((b) => {
              if (b.type === "chart") {
                const raw = b as Record<string, unknown>;
                const { _chartConfig, _chartResult, ...rest } = raw;
                return rest as CanvasBlock;
              }
              return b;
            });
            setBlocks(cleaned);
            setChartConfigs({});
            setChartResults({});
            setSelectedDatasourceId(source.datasourceId ?? null);
            // 注意：canvasId 留空 → 这是「新画布」，不是编辑原画布
            setCanvasId(null);
            setDimensions([]);
            setMeasures([]);
            setFilters([]);
            setChartType("bar");
            setRenderer("echarts");
            setHydrated(true);
            toast.success(`已基于「${source.title}」创建新画布`);
          } catch (err) {
            toast.error("加载模板失败，画布不存在或已删除");
            setHydrated(true);
          }
        })();
        return;
      }
    }

    // 优先加载从报表中心传来的编辑数据
    if (transferData) {
      // 从图表块中提取自动保存时嵌入的 _chartConfig 和 _chartResult
      const extractedConfigs: Record<string, ChartQueryConfig> = {};
      const extractedResults: Record<string, QueryResult> = {};
      const cleanedBlocks = transferData.blocks.map((b) => {
        if (b.type === "chart") {
          const raw = b as Record<string, unknown>;
          const blockId = raw.blockId as string;
          if (blockId && raw._chartConfig) {
            extractedConfigs[blockId] = raw._chartConfig as ChartQueryConfig;
          }
          if (blockId && raw._chartResult) {
            extractedResults[blockId] = raw._chartResult as QueryResult;
          }
          // 清理 _ 前缀的嵌入字段，还原为纯净的块数据
          const { _chartConfig, _chartResult, ...clean } = raw;
          return clean as unknown as CanvasBlock;
        }
        return b;
      });

      setBlocks(cleanedBlocks);
      setChartConfigs(extractedConfigs);
      setChartResults(extractedResults);
      if (transferData.datasourceId) {
        setSelectedDatasourceId(transferData.datasourceId);
      }
      // 重置会话状态，避免旧草稿残留的维度/度量/画布 ID 污染
      setCanvasId(null);
      setDimensions([]);
      setMeasures([]);
      setFilters([]);
      setChartType("bar");
      setRenderer("echarts");
      setHydrated(true);
      return;
    }

    // 回退：从 localStorage 草稿中恢复
    const draft = readDraft();
    if (draft) {
      if (Array.isArray(draft.blocks) && draft.blocks.length > 0) setBlocks(draft.blocks);
      if (draft.chartConfigs && typeof draft.chartConfigs === "object")
        setChartConfigs(draft.chartConfigs);
      if (draft.chartResults && typeof draft.chartResults === "object")
        setChartResults(draft.chartResults);
      if (typeof draft.selectedDatasourceId === "string")
        setSelectedDatasourceId(draft.selectedDatasourceId);
      if (typeof draft.chartType === "string") setChartType(draft.chartType);
      if (Array.isArray(draft.dimensions)) setDimensions(draft.dimensions);
      if (Array.isArray(draft.measures)) setMeasures(draft.measures);
      if (Array.isArray(draft.filters)) setFilters(draft.filters);
      if (typeof draft.renderer === "string") setRenderer(draft.renderer);
      if (typeof draft.canvasId === "string") setCanvasId(draft.canvasId);
    }
    setHydrated(true);
  }, []);

  // 草稿持久化：所有 state 变化 → debounce 300ms → 写 localStorage
  useEffect(() => {
    if (!hydrated) return;
    const timer = window.setTimeout(() => {
      writeDraft({
        blocks,
        chartConfigs,
        chartResults,
        selectedDatasourceId,
        chartType,
        dimensions,
        measures,
        filters,
        renderer,
        canvasId,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [
    hydrated,
    blocks,
    chartConfigs,
    chartResults,
    selectedDatasourceId,
    chartType,
    dimensions,
    measures,
    filters,
    renderer,
    canvasId,
  ]);

  // 后端 autosave：canvasId 存在时，blocks 变化 debounce 1.5s → PUT /canvases/:id/blocks
  useEffect(() => {
    if (!hydrated || !canvasId) return;
    const serialized = JSON.stringify(blocks);
    if (serialized === lastBackendSyncRef.current) return;
    const timer = window.setTimeout(() => {
      // 给每个图表块附加查询配置和结果数据，供后端 PDF 导出使用
      const blocksWithData = blocks.map((b) => {
        if (b.type !== "chart" || !("blockId" in b)) return b;
        const blockId = (b as { blockId: string }).blockId;
        return {
          ...b,
          _chartConfig: chartConfigs[blockId] ?? null,
          _chartResult: chartResults[blockId] ?? null,
        };
      });
      updateCanvasBlocks(canvasId, blocksWithData)
        .then(() => {
          lastBackendSyncRef.current = serialized;
        })
        .catch((e) => {
          console.warn("自动保存画布 blocks 失败:", e);
        });
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [hydrated, canvasId, blocks]);

  // 点击图表块时，同步配置面板显示该块的维度/度量/图表类型
  useEffect(() => {
    if (selectedBlockIdx == null) return;
    const block = blocks[selectedBlockIdx];
    if (!block || block.type !== "chart") return;
    const chartBlock = block as ChartBlock;
    const blockId = chartBlock.blockId;
    const config = chartConfigs[blockId];
    if (!config) return;
    // 同步维度、度量、筛选、图表类型、渲染器
    if (config.dimensions) setDimensions(config.dimensions);
    if (config.measures) setMeasures(config.measures);
    if (config.filters) setFilters(config.filters);
    if (config.chartType) setChartType(config.chartType);
    if (chartBlock.renderer) setRenderer(chartBlock.renderer);
    // 同步数据源（如果块绑定了独立数据源）
    const blockDsId = (chartBlock as unknown as Record<string, unknown>).datasourceId as string | undefined;
    if (blockDsId) setSelectedDatasourceId(blockDsId);
    // 展开配置面板
    setShowConfig(true);
  }, [selectedBlockIdx, blocks, chartConfigs]);

  // 数据源切换时，获取字段元信息（供 AI 助手使用）
  useEffect(() => {
    if (!selectedDatasourceId) {
      setFieldMeta(null);
      return;
    }
    getDatasource(selectedDatasourceId)
      .then((ds) => {
        const fields = ds.schemaMeta?.fields;
        if (Array.isArray(fields)) {
          setFieldMeta(
            fields.map((f) => ({
              name: String(f.name || ""),
              data_type: String(f.dataType || "VARCHAR"),
              category: f.category as string | undefined,
            }))
          );
        } else {
          setFieldMeta(null);
        }
      })
      .catch(() => setFieldMeta(null));
  }, [selectedDatasourceId]);

  // 获取所有数据源列表（供 AI 助手使用）
  useEffect(() => {
    listDatasources({ pageSize: 100 }).then((res) => {
      const list = (res.items || []).map((ds) => ({
        id: ds.id,
        name: ds.name,
        fields: (ds.schemaMeta?.fields || []).map((f) => ({
          name: f.name,
          data_type: f.dataType,
        })),
      }));
      setDatasourceList(list);
    }).catch(() => {});
  }, []);

  const ensureCanvas = async (): Promise<string> => {
    if (canvasId) return canvasId;
    if (!selectedDatasourceId) {
      throw new Error("请先选择数据源");
    }
    const created = await createCanvas({
      title: "分析画布",
      datasourceId: selectedDatasourceId,
    });
    setCanvasId(created.id);
    return created.id;
  };

  // 切换数据源时重置 canvasId，避免 canvas 绑定到旧数据源
  const handleSelectDatasource = (id: string) => {
    if (id !== selectedDatasourceId) {
      setCanvasId(null);
    }
    setSelectedDatasourceId(id);
  };

  const handleApply = async () => {
    if (!selectedDatasourceId) {
      toast.warning("请先选择数据源");
      return;
    }
    if (dimensions.length === 0 || measures.length === 0) {
      toast.warning("请至少添加一个维度和一个度量");
      return;
    }
    setApplying(true);
    try {
      const selectedBlock =
        selectedBlockIdx != null ? blocks[selectedBlockIdx] : null;
      const selectedIsChart =
        selectedBlock != null &&
        selectedBlock.type === "chart" &&
        typeof (selectedBlock as { blockId?: unknown }).blockId === "string";

      // 先确定要用哪个数据源，再统一构建 config 和执行查询
      const blockDsId = selectedIsChart
        ? (selectedBlock as Record<string, unknown>).datasourceId as string | undefined
        : undefined;
      const applyDsId = blockDsId || selectedDatasourceId;

      const id = await ensureCanvas();
      const config: ChartQueryConfig = {
        dimensions,
        measures,
        filters,
        chartType: chartType,
        limit: 20,
        datasourceId: applyDsId,
      };
      const result = await executeChartQuery(id, config);

      if (selectedIsChart) {
        const existingBlockId = (selectedBlock as { blockId: string }).blockId;
        setChartConfigs((prev) => ({ ...prev, [existingBlockId]: config }));
        setChartResults((prev) => ({ ...prev, [existingBlockId]: result }));
        setBlocks((prev) =>
          prev.map((b, i) =>
            i === selectedBlockIdx
              ? { ...b, title: `${chartType} 图表`, renderer, datasourceId: applyDsId }
              : b
          )
        );
      } else {
        const blockId = `chart_${Date.now()}`;
        setChartConfigs((prev) => ({ ...prev, [blockId]: config }));
        setChartResults((prev) => ({ ...prev, [blockId]: result }));
        setBlocks((prev) => [
          ...prev,
          { type: "chart", blockId: blockId, title: `${chartType} 图表`, renderer, datasourceId: applyDsId },
        ]);
      }
    } catch (e: unknown) {
      // 从多种后端错误格式中提取消息
      const err = e as {
        response?: { data?: { detail?: { message?: string } | string; error?: { message?: string } } };
        message?: string;
      };
      const detail = err?.response?.data?.detail;
      const detailMsg = typeof detail === "string" ? detail : detail?.message;
      const errorMsg = err?.response?.data?.error?.message;
      const msg = detailMsg || errorMsg || err?.message;
      console.error("应用配置失败:", msg, err?.response?.data || e);
      toast.error(msg ? `查询失败: ${msg}` : "应用失败：未连接到后端或数据源为空");
    } finally {
      setApplying(false);
    }
  };

  const handleAddDimension = (field: string) => {
    setDimensions((prev) => (prev.includes(field) ? prev : [...prev, field]));
  };

  const handleAddMeasure = (field: string) => {
    setMeasures((prev) =>
      prev.some((m) => m.field === field) ? prev : [...prev, { field, agg: "SUM" }]
    );
  };

  const handleAddFilter = (field: string) => {
    setFilters((prev) => {
      if (prev.some((f) => f.field === field)) return prev;
      return [...prev, { field, op: "between", value: ["", ""] }];
    });
  };

  const handleChangeMeasureAgg = (index: number, agg: MeasureConfig["agg"]) => {
    setMeasures((prev) =>
      prev.map((m, i) => (i === index ? { ...m, agg } : m))
    );
  };

  const handleReset = () => {
    setDimensions([]);
    setMeasures([]);
    setFilters([]);
    setChartType("bar");
  };

  const handleAddTextBlockAtIdx = (index: number, block: CanvasBlock) => {
    setBlocks((prev) => {
      const next = [...prev];
      next.splice(index + 1, 0, block);
      return next;
    });
  };

  const handleUpdateTextBlockContent = (index: number, content: string) => {
    setBlocks((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], content };
      return next;
    });
  };

  const handleUpdateBlock = (index: number, patch: Record<string, unknown>) => {
    setBlocks((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], ...patch };
      return next;
    });
  };

  const handleReorderBlocks = (fromIdx: number, toIdx: number) => {
    setBlocks((prev) => {
      if (fromIdx < 0 || fromIdx >= prev.length) return prev;
      const next = [...prev];
      const [moved] = next.splice(fromIdx, 1);
      const adjusted = fromIdx < toIdx ? toIdx - 1 : toIdx;
      next.splice(Math.max(0, Math.min(next.length, adjusted)), 0, moved);
      return next;
    });
  };

  const handlePaletteChange = (id: string) => {
    setRenderer((prev) => prev);
    setBlocks((prev) => {
      const idx = selectedBlockIdx;
      if (idx == null) return prev;
      const target = prev[idx];
      if (!target || target.type !== "chart") return prev;
      const next = [...prev];
      next[idx] = { ...target, palette: id };
      return next;
    });
  };

  const handleAddTextBlock = () => {
    setBlocks((prev) => [...prev, { type: "text", content: "新文本块..." }]);
  };

  const handleAddH1Block = () => {
    setBlocks((prev) => [...prev, { type: "h1", content: "新标题" }]);
  };

  const handleAddDividerBlock = () => {
    setBlocks((prev) => [...prev, { type: "divider" }]);
  };

  const handleAddImageBlock = () => {
    setBlocks((prev) => [...prev, { type: "image", src: "" }]);
  };

  /** AI 画布助手推荐图表配置时，直接应用到画布 */
  const handleAIChatConfig = async (config: {
    chartType?: string;
    dimensions?: string[];
    measures?: Array<{ field: string; agg: string }>;
  }) => {
    if (!selectedDatasourceId) {
      toast.warning("AI 需要选择数据源才能生成图表");
      return;
    }
    const dims = config.dimensions || [];
    const meas = config.measures || [];
    if (dims.length === 0 || meas.length === 0) return;

    setDimensions(dims);
    setMeasures(meas as MeasureConfig[]);
    if (config.chartType) setChartType(config.chartType as typeof chartType);
    setSelectedBlockIdx(null);

    try {
      const id = await ensureCanvas();
      const queryConfig: ChartQueryConfig = {
        dimensions: dims,
        measures: meas as MeasureConfig[],
        filters: [],
        chartType: (config.chartType || chartType) as typeof chartType,
        limit: 20,
        datasourceId: selectedDatasourceId,
      };
      const result = await executeChartQuery(id, queryConfig);
      const blockId = `chart_${Date.now()}`;
      const ct = config.chartType || chartType;
      setChartConfigs((prev) => ({ ...prev, [blockId]: queryConfig }));
      setChartResults((prev) => ({ ...prev, [blockId]: result }));
      setBlocks((prev) => [
        ...prev,
        { type: "chart", blockId, title: `${ct} 图表`, renderer: "echarts", datasourceId: selectedDatasourceId },
      ]);
      toast.success("AI 已生成图表");
    } catch (e: any) {
      const msg = typeof e === "string" ? e : e?.message || "未知错误";
      toast.error(`AI 图表生成失败: ${msg}`);
    }
  };

  const handleAddChartBlock = () => {
    const blockId = `chart_${Date.now()}`;
    const config: ChartQueryConfig = {
      dimensions,
      measures,
      filters,
      chartType: chartType,
      limit: 20,
    };
    setChartConfigs((prev) => ({ ...prev, [blockId]: config }));
    setBlocks((prev) => [
      ...prev,
      { type: "chart", blockId: blockId, title: `${chartType} 图表`, renderer, datasourceId: selectedDatasourceId },
    ]);
  };

  const handleDeleteBlock = (index: number) => {
    setBlocks((prev) => {
      const removed = prev[index];
      if (removed?.type === "chart" && "blockId" in removed) {
        const blockId = (removed as { blockId: string }).blockId;
        setChartConfigs((prevConfigs) => {
          const next = { ...prevConfigs };
          delete next[blockId];
          return next;
        });
        setChartResults((prevResults) => {
          const next = { ...prevResults };
          delete next[blockId];
          return next;
        });
      }
      return prev.filter((_, i) => i !== index);
    });
    setSelectedBlockIdx((prev) => {
      if (prev == null) return prev;
      if (prev === index) return null;
      if (prev > index) return prev - 1;
      return prev;
    });
  };

  const openDashboardModal = async () => {
    // 如果还没有 canvas，自动创建一个
    if (!canvasId) {
      try {
        await ensureCanvas();
      } catch {
        toast.warning("请先选择数据源并应用图表配置");
        return;
      }
    }
    // 检查有没有图表块
    if (!blocks.some((b) => b.type === "chart")) {
      toast.warning("当前画布没有图表块，请先拖入字段并应用配置");
      return;
    }
    setShowDashboardModal(true);
    setDashboardError(null);
    setDashboardSuccess(null);
    setSelectedDashboardId(null);
    setCreatingDashboard(false);
    setNewDashboardTitle("");
    setDashboardListLoading(true);
    try {
      const result = await listDashboards({ pageSize: 100 });
      setDashboardList(result.items);
    } catch (e) {
      const msg =
        e instanceof Error ? e.message : typeof e === "string" ? e : "加载仪表盘列表失败";
      setDashboardListError(msg);
    } finally {
      setDashboardListLoading(false);
    }
  };

  const closeDashboardModal = () => {
    setShowDashboardModal(false);
    setDashboardError(null);
    setDashboardSuccess(null);
    setSelectedDashboardId(null);
    setCreatingDashboard(false);
    setNewDashboardTitle("");
  };

  const handleSaveToDashboard = async () => {
    // 确保有 canvasId（防御性检查，避免 state 更新延迟）
    let currentCanvasId = canvasId;
    if (!currentCanvasId) {
      try {
        currentCanvasId = await ensureCanvas();
      } catch {
        setDashboardError("请先选择数据源并应用图表配置");
        return;
      }
    }
    let dashboardId = selectedDashboardId;
    if (creatingDashboard) {
      if (!newDashboardTitle.trim()) {
        setDashboardError("请输入新仪表盘标题");
        return;
      }
      try {
        const created = await createDashboard({ title: newDashboardTitle.trim() });
        dashboardId = created.id;
      } catch (e) {
        const msg =
          e instanceof Error ? e.message : typeof e === "string" ? e : "创建仪表盘失败";
        setDashboardError(msg);
        return;
      }
    }
    if (!dashboardId) {
      setDashboardError("请选择已有仪表盘或新建一个");
      return;
    }

    setDashboardSaving(true);
    setDashboardError(null);
    setDashboardSuccess(null);
    try {
      const chartBlocks = blocks.filter(
        (b): b is CanvasBlock & { type: "chart"; blockId: string } =>
          b.type === "chart" &&
          typeof (b as { blockId?: unknown }).blockId === "string"
      );
      if (chartBlocks.length === 0) {
        setDashboardError("当前画布没有图表块，请先应用配置生成图表");
        setDashboardSaving(false);
        return;
      }
      let addedCount = 0;
      for (const [i, block] of chartBlocks.entries()) {
        const blockId = block.blockId;
        const config = chartConfigs[blockId];
        if (!config) continue;
        // 构造包含 chart_type 的完整配置
        const chartConfigPayload: Record<string, unknown> = {
          chart_type: config.chartType || "bar",
          query_config: config,
          renderer: (block as Record<string, unknown>).renderer || "recharts",
          palette: (block as Record<string, unknown>).palette,
        };
        await pinCanvasToDashboard(currentCanvasId, {
          dashboard_id: dashboardId,
          chart_config: chartConfigPayload,
          position: { x: 0, y: i, w: 6, h: 4 },
        });
        addedCount += 1;
      }
      setDashboardSuccess(`已添加 ${addedCount} 个图表到仪表盘`);
      setSelectedDashboardId(null);
      setCreatingDashboard(false);
      setNewDashboardTitle("");
    } catch (e) {
      const err = e as { response?: { data?: { detail?: { message?: string } | string } }; message?: string };
      const detail = err?.response?.data?.detail;
      const detailMsg = typeof detail === "string" ? detail : detail?.message;
      const msg = detailMsg || err?.message || "保存到仪表盘失败";
      setDashboardError(msg);
    } finally {
      setDashboardSaving(false);
    }
  };

  const openReportModal = () => {
    if (!canvasId) {
      toast.warning("请先应用配置生成图表");
      return;
    }
    setShowReportModal(true);
    setReportError(null);
    setReportSuccess(null);
    setReportTitle("分析报告");
    setReportStatus("draft");
  };

  const closeReportModal = () => {
    setShowReportModal(false);
    setReportError(null);
    setReportSuccess(null);
  };

  const handleSaveToReport = async () => {
    if (!canvasId) {
      setReportError("请先应用配置生成图表");
      return;
    }
    if (!reportTitle.trim()) {
      setReportError("请输入报表标题");
      return;
    }
    setReportSaving(true);
    setReportError(null);
    setReportSuccess(null);
    try {
      const result = await saveCanvasAsReport(canvasId, {
        title: reportTitle.trim(),
        status: reportStatus,
      });
      setReportSuccess(`报表已创建：${result.report_id}`);
    } catch (e) {
      const msg =
        e instanceof Error ? e.message : typeof e === "string" ? e : "创建报表失败";
      setReportError(msg);
    } finally {
      setReportSaving(false);
    }
  };

  const handleExportPdf = async () => {
    if (!canvasId) {
      toast.warning("请先应用配置生成图表");
      return;
    }
    if (exportingPdf) return;
    setExportingPdf(true);
    try {
      const blob = await exportCanvasPdf(canvasId);
      // 根据实际内容类型决定文件后缀，默认使用 .pdf
      const isPdf = blob.type === "application/pdf";
      const ext = isPdf ? ".pdf" : ".pdf";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `canvas${ext}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error("导出失败:", e);
      toast.error("导出失败");
    } finally {
      setExportingPdf(false);
    }
  };

  const handleResetCanvas = async () => {
    const hasRemote = !!canvasId;
    const ok = await toast.confirm(
      hasRemote
        ? "确定要删除当前画布吗？画布将进入回收站，可在回收站中恢复。"
        : "确定要清空当前画布的全部块和配置吗？"
    );
    if (!ok) return;
    // 已保存到后端的画布：调用 DELETE 软删除到回收站
    if (hasRemote) {
      try {
        await deleteCanvas(canvasId);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "删除画布失败";
        toast.error(msg);
        return;
      }
    }
    setBlocks([]);
    setChartConfigs({});
    setChartResults({});
    setDimensions([]);
    setMeasures([]);
    setFilters([]);
    setChartType("bar");
    setRenderer("echarts");
    setCanvasId(null);
    try {
      window.localStorage.removeItem(CANVAS_DRAFT_KEY);
    } catch {
      // ignore
    }
    if (hasRemote) {
      toast.success("画布已移入回收站");
    }
  };

  const toolbarButtons: Array<{
    icon: string;
    label: string;
    onClick?: () => void;
    dragType?: string;
  }> = [
    { icon: "H", label: "标题块", onClick: handleAddH1Block, dragType: "h1" },
    { icon: "T", label: "文本块", onClick: handleAddTextBlock, dragType: "text" },
    { icon: "—", label: "分割线", onClick: handleAddDividerBlock, dragType: "divider" },
    { icon: "■", label: "图片块", onClick: handleAddImageBlock, dragType: "image" },
    { icon: "■", label: "图表块", onClick: handleAddChartBlock, dragType: "chart" },
  ];

  const [draggedType, setDraggedType] = useState<string | null>(null);

  const handleDragStart = (
    e: React.DragEvent<HTMLButtonElement>,
    dragType: string
  ) => {
    e.dataTransfer.setData("application/x-lvco-block", dragType);
    e.dataTransfer.setData("text/plain", dragType);
    e.dataTransfer.effectAllowed = "copy";
    setDraggedType(dragType);
  };

  const handleDragEnd = () => {
    setDraggedType(null);
  };

  const flashToolbarButton = (dragType: string) => {
    setRecentlyClickedBtn(dragType);
    window.setTimeout(() => {
      setRecentlyClickedBtn((current) => (current === dragType ? null : current));
    }, 450);
  };

  const handleCanvasDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };

  const handleCanvasDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    let dragType = e.dataTransfer.getData("application/x-lvco-block");
    if (!dragType) dragType = e.dataTransfer.getData("text/plain");
    if (!dragType) {
      setDraggedType(null);
      return;
    }
    if (dragType === "h1") setBlocks((prev) => [...prev, { type: "h1", content: "新标题" }]);
    else if (dragType === "text") setBlocks((prev) => [...prev, { type: "text", content: "新文本块..." }]);
    else if (dragType === "divider") setBlocks((prev) => [...prev, { type: "divider" }]);
    else if (dragType === "image") setBlocks((prev) => [...prev, { type: "image", src: "" }]);
    else if (dragType === "chart") {
      const blockId = `chart_${Date.now()}`;
      const config: ChartQueryConfig = {
        dimensions,
        measures,
        filters,
        chartType: chartType,
        limit: 20,
      };
      setChartConfigs((prev) => ({ ...prev, [blockId]: config }));
      setBlocks((prev) => [
        ...prev,
        { type: "chart", blockId: blockId, title: `${chartType} 图表`, renderer, datasourceId: selectedDatasourceId },
      ]);
    }
    setDraggedType(null);
  };

  return (
    <>
      <header className="h-14 flex items-center justify-between px-5 border-b bg-white flex-shrink-0 border-border">
        <div className="flex items-center gap-3">
          {editingTitle ? (
            <form
              className="flex items-center gap-1"
              onSubmit={(e) => {
                e.preventDefault();
                setEditingTitle(false);
                const newTitle = canvasTitle.trim() || "分析画布";
                setCanvasTitle(newTitle);
                if (canvasId) {
                  updateCanvas(canvasId, { title: newTitle }).catch(() => {});
                }
              }}
            >
              <input
                ref={titleInputRef}
                value={canvasTitle}
                onChange={(e) => setCanvasTitle(e.target.value)}
                className="text-[15px] font-semibold text-foreground bg-transparent border-b-2 border-primary outline-none px-1 py-0 w-[200px]"
                autoFocus
                onBlur={() => {
                  setEditingTitle(false);
                  const newTitle = canvasTitle.trim() || "分析画布";
                  setCanvasTitle(newTitle);
                  if (canvasId) {
                    updateCanvas(canvasId, { title: newTitle }).catch(() => {});
                  }
                }}
              />
              <button type="submit" className="text-primary">
                <Check className="w-4 h-4" />
              </button>
            </form>
          ) : (
            <>
              <span className="text-[15px] font-semibold text-foreground max-w-[300px] truncate">
                {canvasTitle}
              </span>
              <Pencil
                className="w-3.5 h-3.5 text-muted-foreground cursor-pointer hover:text-primary flex-shrink-0"
                onClick={() => {
                  setEditingTitle(true);
                  setTimeout(() => titleInputRef.current?.select(), 50);
                }}
              />
            </>
          )}

          {/* 最近画布 */}
          <div className="relative ml-2">
            <button
              onClick={async () => {
                setShowRecentCanvases(!showRecentCanvases);
                if (!showRecentCanvases) {
                  try {
                    const res = await listCanvases({ page: 1, pageSize: 10 });
                    setRecentCanvases((res.items ?? []).map(c => ({
                      id: c.id,
                      title: c.title,
                      updatedAt: c.updatedAt ?? null,
                    })));
                  } catch {}
                }
              }}
              className="flex items-center gap-1 px-2 py-1 rounded-[6px] text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              title="最近画布"
            >
              <History className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">最近</span>
            </button>
            {showRecentCanvases && (
              <div className="absolute top-full left-0 mt-1 w-[240px] bg-white border border-border-light rounded-[8px] shadow-lg z-50 max-h-[280px] overflow-y-auto">
                <div className="px-3 py-2 text-[11px] font-semibold text-muted-foreground/70 border-b border-border-light flex items-center justify-between">
                  <span>最近画布</span>
                  <button
                    onClick={async () => {
                      // 把当前可见的画布（除当前打开的）全部软删除到回收站，
                      // 这样下次打开"最近"时它们就不会再出现了
                      const toDelete = recentCanvases.filter(c => !hiddenIds.has(c.id) && c.id !== canvasId);
                      for (const c of toDelete) {
                        try {
                          await deleteCanvas(c.id);
                          hideCanvasId(c.id);
                        } catch {
                          // 单个失败不阻塞其他
                        }
                      }
                      setHiddenIds(new Set(getHiddenCanvasIds()));
                      setRecentCanvases([]);
                    }}
                    className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-danger hover:bg-danger-light transition-colors"
                    title="清空历史（画布将移入回收站）"
                  >
                    <Trash2 className="w-3 h-3" />
                    清空
                  </button>
                </div>
                {(() => {
                  const filtered = recentCanvases.filter(c => !hiddenIds.has(c.id));
                  if (filtered.length === 0) {
                    return (
                      <div className="px-3 py-4 text-center text-[12px] text-muted-foreground">
                        暂无最近画布
                      </div>
                    );
                  }
                  return filtered.map((c) => (
                    <div
                      key={c.id}
                      className={`flex items-center group/item hover:bg-muted transition-colors ${
                        c.id === canvasId ? 'bg-primary-light/30' : ''
                      }`}
                    >
                      <button
                        onClick={() => {
                          setShowRecentCanvases(false);
                          window.location.href = `/?template=${encodeURIComponent(c.id)}`;
                        }}
                        className="flex-1 text-left px-3 py-2 text-[12px] flex items-center justify-between min-w-0"
                      >
                        <span className={`truncate ${c.id === canvasId ? 'text-primary' : 'text-foreground'}`}>
                          {c.title}
                        </span>
                        <span className="text-[10px] text-muted-foreground ml-2 flex-shrink-0">
                          {c.updatedAt ? new Date(c.updatedAt).toLocaleDateString('zh-CN') : ''}
                        </span>
                      </button>
                      <button
                        onClick={async (e) => {
                          e.stopPropagation();
                          // 软删除画布到回收站，并在隐藏列表中标记
                          try {
                            await deleteCanvas(c.id);
                          } catch {
                            // 即使删除失败也要本地隐藏，避免重复显示
                          }
                          hideCanvasId(c.id);
                          setHiddenIds(prev => {
                            const next = new Set(prev);
                            next.add(c.id);
                            return next;
                          });
                        }}
                        className="px-2 py-2 text-muted-foreground hover:text-danger opacity-0 group-hover/item:opacity-100 transition-opacity flex-shrink-0"
                        title="从列表中移除"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  ));
                })()}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          {selectedDatasourceId ? (
            <span className="text-[12px] text-muted-foreground">
              已绑定数据源
            </span>
          ) : (
            <span className="text-[12px] text-muted-foreground">
              请在左侧选择数据源
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={openDashboardModal}
            disabled={!selectedDatasourceId}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium text-white bg-primary disabled:opacity-50"
          >
            <Pin className="w-3.5 h-3.5" />
            保存到仪表盘
          </button>
          <button
            onClick={openReportModal}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium border border-primary text-primary bg-white"
          >
            <FileOutput className="w-3.5 h-3.5" />
            保存到报表
          </button>
          <button
            onClick={handleExportPdf}
            disabled={!canvasId || exportingPdf}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium border border-primary text-primary bg-white disabled:opacity-50"
          >
            {exportingPdf ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Download className="w-3.5 h-3.5" />
            )}
            {exportingPdf ? "导出中..." : "导出 PDF"}
          </button>
          <button
            onClick={handleResetCanvas}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium text-muted-foreground hover:text-danger border border-border bg-white"
            title={canvasId ? "删除画布（进入回收站）" : "清空本地画布草稿，重置为空白"}
          >
            {canvasId ? "删除画布" : "清空画布"}
          </button>
        </div>
      </header>

      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        <div className={`${showFields ? 'block' : 'hidden'} md:block`}>
          <FieldPanel
            selectedDatasourceId={selectedDatasourceId}
            onSelectDatasource={handleSelectDatasource}
            onAddDimension={handleAddDimension}
            onAddMeasure={handleAddMeasure}
            onAddFilter={handleAddFilter}
            collapsed={fieldsCollapsed}
            onToggleCollapsed={() => setFieldsCollapsed((prev) => !prev)}
          />
        </div>

        <div
          className={`flex-1 overflow-auto p-6 relative transition-colors duration-150 ${
            draggedType
              ? "bg-primary-light/40 ring-2 ring-dashed ring-primary/40 ring-inset"
              : ""
          }`}
          style={{
            backgroundImage:
              "radial-gradient(circle, #E2E8F0 1px, transparent 1px)",
            backgroundSize: "20px 20px",
          }}
          onDragOver={handleCanvasDragOver}
          onDrop={handleCanvasDrop}
        >
          <div className="md:hidden flex gap-2 mb-2">
            <button
              onClick={() => setShowFields(!showFields)}
              className="px-3 py-1 text-xs border rounded"
            >
              {showFields ? '隐藏字段' : '字段'}
            </button>
            <button
              onClick={() => setShowConfig(!showConfig)}
              className="px-3 py-1 text-xs border rounded"
            >
              {showConfig ? '隐藏配置' : '配置'}
            </button>
          </div>
          <div className="flex items-center gap-1 px-3 py-2.5 mb-6 bg-white rounded-[10px] shadow-card">
            {toolbarButtons.map(({ icon, label, onClick, dragType }) => (
              <button
                key={label}
                onClick={() => {
                  if (!dragType) {
                    onClick?.();
                    return;
                  }
                  flashToolbarButton(dragType);
                  onClick?.();
                }}
                draggable={true}
                onDragStart={(e) => dragType && handleDragStart(e, dragType)}
                onDragEnd={handleDragEnd}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-[6px] text-[12px] font-medium bg-muted text-card-foreground hover:bg-muted/80 active:scale-95 active:bg-muted transition-all duration-150 cursor-grab active:cursor-grabbing ${
                  draggedType === dragType ? "opacity-50 ring-2 ring-primary/50" : ""
                } ${recentlyClickedBtn === dragType ? "ring-2 ring-success/60 bg-success/15" : ""}`}
                title="点击添加，或拖拽到画布"
              >
                <span
                  className={
                    icon.length === 1 ? "font-bold text-[13px]" : "text-[13px]"
                  }
                >
                  {icon}
                </span>
                <span className="text-[11px]">{label}</span>
              </button>
            ))}
          </div>
          <CanvasBlocks
            blocks={blocks}
            chartConfigs={chartConfigs}
            chartResults={chartResults}
            loadingCharts={{}}
            onDeleteBlock={handleDeleteBlock}
            datasourceId={selectedDatasourceId}
            onInsertBlockAfter={handleAddTextBlockAtIdx}
            onUpdateBlockContent={handleUpdateTextBlockContent}
            onUpdateBlock={handleUpdateBlock}
            onReorderBlocks={handleReorderBlocks}
            selectedBlockIdx={selectedBlockIdx}
            onSelectBlock={setSelectedBlockIdx}
          />
        </div>

        <div className={`${showConfig ? 'block' : 'hidden'} md:block`}>
          <ConfigPanel
            chartType={chartType}
            onChartTypeChange={setChartType}
            dimensions={dimensions}
            measures={measures}
            filters={filters}
            onRemoveDimension={(i) =>
              setDimensions((prev) => prev.filter((_, idx) => idx !== i))
            }
            onRemoveMeasure={(i) =>
              setMeasures((prev) => prev.filter((_, idx) => idx !== i))
            }
            onRemoveFilter={(i) =>
              setFilters((prev) => prev.filter((_, idx) => idx !== i))
            }
            onChangeMeasureAgg={handleChangeMeasureAgg}
            onApply={handleApply}
            onReset={handleReset}
            applying={applying}
            canvasId={canvasId}
            renderer={renderer}
            onRendererChange={setRenderer}
            applyMode={
              selectedBlockIdx != null &&
              blocks[selectedBlockIdx]?.type === "chart"
                ? "update"
                : "create"
            }
            onClearSelection={() => setSelectedBlockIdx(null)}
            onDropField={(payload) => {
              if (payload.category === "measure") handleAddMeasure(payload.name);
              else if (payload.category === "time") handleAddFilter(payload.name);
              else handleAddDimension(payload.name);
            }}
            palette={
              selectedBlockIdx != null &&
              blocks[selectedBlockIdx]?.type === "chart" &&
              typeof (blocks[selectedBlockIdx] as { palette?: unknown }).palette === "string"
                ? ((blocks[selectedBlockIdx] as { palette: string }).palette)
                : "default"
            }
            onPaletteChange={handlePaletteChange}
            collapsed={configCollapsed}
            onToggleCollapsed={() => setConfigCollapsed((prev) => !prev)}
            onSetDimensions={setDimensions}
            onSetMeasures={setMeasures}
            datasourceId={selectedDatasourceId}
          />
        </div>
      </div>

      <AIAssistant
        datasourceId={selectedDatasourceId}
        fieldMeta={fieldMeta}
        currentDimensions={dimensions}
        currentMeasures={measures}
        currentChartType={chartType}
        allDatasources={datasourceList}
        onApplyChartConfig={handleAIChatConfig}
      />

      <ModalShell
        open={showDashboardModal}
        onClose={closeDashboardModal}
        title="保存到仪表盘"
        footer={
          <>
            <button
              type="button"
              onClick={closeDashboardModal}
              disabled={dashboardSaving}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSaveToDashboard}
              disabled={
                dashboardSaving ||
                (!selectedDashboardId && !creatingDashboard)
              }
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {dashboardSaving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Pin className="w-4 h-4" />
              )}
              保存
            </button>
          </>
        }
      >
        <div className="space-y-4 text-[13px]">
          <div>
            <p className="text-[12px] text-muted-foreground mb-2">选择已有仪表盘</p>
            {dashboardListLoading ? (
              <div className="px-3 py-4 text-center text-[13px] text-muted-foreground border border-border-light rounded-md">
                <span className="inline-flex items-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  正在加载仪表盘...
                </span>
              </div>
            ) : dashboardListError ? (
              <div className="px-3 py-2 rounded-md bg-danger-light text-danger">
                {dashboardListError}
              </div>
            ) : dashboardList.length === 0 ? (
              <div className="px-3 py-4 text-center text-[13px] text-muted-foreground border border-border-light rounded-md">
                暂无仪表盘，请在下方新建
              </div>
            ) : (
              <div className="max-h-[180px] overflow-y-auto border border-border-light rounded-md divide-y divide-border-light">
                {dashboardList.map((d) => (
                  <label
                    key={d.id}
                    className={`flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-muted transition-colors ${
                      selectedDashboardId === d.id && !creatingDashboard
                        ? "bg-primary-light"
                        : ""
                    }`}
                  >
                    <input
                      type="radio"
                      name="dashboard-select"
                      checked={selectedDashboardId === d.id && !creatingDashboard}
                      onChange={() => {
                        setSelectedDashboardId(d.id);
                        setCreatingDashboard(false);
                      }}
                      disabled={creatingDashboard}
                      className="accent-primary"
                    />
                    <span className="text-card-foreground flex-1 truncate">
                      {d.title}
                    </span>
                    <span className="text-[12px] text-muted-foreground">
                      {d.chartCount} 个图表
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>

          <div className="border-t border-border-light pt-3">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="dashboard-select"
                checked={creatingDashboard}
                onChange={() => {
                  setCreatingDashboard(true);
                  setSelectedDashboardId(null);
                }}
                className="accent-primary"
              />
              <span className="text-card-foreground font-medium">+ 新建仪表盘</span>
            </label>
            {creatingDashboard ? (
              <input
                type="text"
                value={newDashboardTitle}
                onChange={(e) => setNewDashboardTitle(e.target.value)}
                placeholder="输入新仪表盘标题"
                className="mt-2 w-full px-3 py-2 text-[13px] rounded-md border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
              />
            ) : null}
          </div>

          {dashboardError ? (
            <div className="px-3 py-2 rounded-md bg-danger-light text-danger">
              {dashboardError}
            </div>
          ) : null}
          {dashboardSuccess ? (
            <div className="px-3 py-2 rounded-md bg-success-light text-success inline-flex items-center gap-1.5">
              <CheckCircle2 className="w-4 h-4" />
              {dashboardSuccess}
            </div>
          ) : null}
        </div>
      </ModalShell>

      <ModalShell
        open={showReportModal}
        onClose={closeReportModal}
        title="保存到报表"
        footer={
          <>
            <button
              type="button"
              onClick={closeReportModal}
              disabled={reportSaving}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSaveToReport}
              disabled={reportSaving}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {reportSaving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <FileOutput className="w-4 h-4" />
              )}
              保存
            </button>
          </>
        }
      >
        <div className="space-y-3 text-[13px]">
          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">
              报表标题
            </label>
            <input
              type="text"
              value={reportTitle}
              onChange={(e) => setReportTitle(e.target.value)}
              placeholder="输入报表标题"
              className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            />
          </div>
          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">
              状态
            </label>
            <select
              value={reportStatus}
              onChange={(e) => setReportStatus(e.target.value as ReportStatus)}
              className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
            >
              <option value="draft">草稿</option>
              <option value="published">已发布</option>
              <option value="shared">已分享</option>
              <option value="archived">已归档</option>
            </select>
          </div>
          <p className="text-[12px] text-muted-foreground">
            报表将快照当前画布的全部块（含已渲染的图表），后续可在报表中心查看。
          </p>
          {reportError ? (
            <div className="px-3 py-2 rounded-md bg-danger-light text-danger">
              {reportError}
            </div>
          ) : null}
          {reportSuccess ? (
            <div className="px-3 py-2 rounded-md bg-success-light text-success inline-flex items-center gap-1.5">
              <CheckCircle2 className="w-4 h-4" />
              {reportSuccess}
            </div>
          ) : null}
        </div>
      </ModalShell>
    </>
  );
}