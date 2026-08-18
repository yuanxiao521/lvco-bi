import { useNavigate } from "react-router-dom";
import {
  LayoutTemplate,
  Sparkles,
  FileText,
  TrendingUp,
  Users,
  Activity,
  Wallet,
  ArrowRight,
  BarChart3,
} from "lucide-react";
import { DEFAULT_TEMPLATES, type DefaultTemplate } from "../../data/defaultTemplates";

const ICON_MAP = { TrendingUp, Users, Activity, Wallet } as const;
const COLOR_CLASS = {
  primary: "from-primary to-primary/70",
  info: "from-info to-info/70",
  success: "from-success to-success/70",
  warning: "from-warning to-warning/70",
} as const;
/**
 * 模板库
 * - 显示系统内置模板（不依赖用户数据）
 * - 点击系统模板：跳转 /?template=system-* 由 FreeCanvas 加载
 */
export default function TemplatesPage() {
  const navigate = useNavigate();

  const handleSelect = (id: string) => {
    navigate(`/?template=${encodeURIComponent(id)}`);
  };

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <div className="px-8 pt-8 pb-6">
        <h1 className="text-[20px] font-bold text-foreground flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center">
            <LayoutTemplate className="w-4 h-4 text-white" />
          </div>
          模板库
        </h1>
        <p className="text-[13px] text-muted-foreground mt-2 ml-[42px]">
          基于系统模板或已有画布快速复用，避免从零开始
        </p>
      </div>

      {/* 默认模板 */}
      <div className="px-8 pb-10">
        <div className="flex items-center gap-2 mb-5">
          <div className="w-6 h-6 rounded-md bg-gradient-to-br from-amber-400 to-orange-400 flex items-center justify-center">
            <Sparkles className="w-3 h-3 text-white" />
          </div>
          <h2 className="text-[15px] font-semibold text-foreground">默认模板</h2>
          <span className="text-[11px] text-muted-foreground ml-1">由系统提供，开箱即用。选择数据源后即可生成图表</span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
          {/* 空白模板 */}
          <TemplateCard
            title="空白模板"
            desc="从零开始，构建你的第一份分析报告"
            icon={FileText}
            color="default"
            onClick={() => handleSelect("system-blank")}
            tag="推荐起步"
            chartCount={0}
            fieldCount={0}
          />
          {DEFAULT_TEMPLATES.filter(t => t.id !== "system-blank").map((tpl) => (
            <DefaultTemplateCard
              key={tpl.id}
              tpl={tpl}
              onClick={() => handleSelect(tpl.id)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function TemplateCard({
  title,
  desc,
  icon: Icon,
  color,
  onClick,
  tag,
  chartCount,
  fieldCount,
}: {
  title: string;
  desc: string;
  icon: typeof FileText;
  color: keyof typeof COLOR_CLASS | "default";
  onClick: () => void;
  tag?: string;
  chartCount?: number;
  fieldCount?: number;
}) {
  return (
    <button
      onClick={onClick}
      className="group text-left bg-white rounded-xl border border-border-light/80 hover:border-primary/30 hover:shadow-lg hover:-translate-y-1 transition-all duration-300 relative overflow-hidden"
    >
      {/* 顶部渐变条 */}
      <div className={`h-1.5 ${color === "default" ? "bg-gradient-to-r from-primary to-primary/60" : `bg-gradient-to-r ${COLOR_CLASS[color as keyof typeof COLOR_CLASS]}`}`} />

      <div className="p-5">
        {tag && (
          <span className="absolute top-3 right-3 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-gradient-to-r from-primary to-primary/80 text-white shadow-sm">
            {tag}
          </span>
        )}
        <div
          className={`w-11 h-11 rounded-xl flex items-center justify-center mb-3.5 ${
            color === "default"
              ? "bg-gradient-to-br from-primary to-primary/70 shadow-md shadow-primary/20"
              : `bg-gradient-to-br ${COLOR_CLASS[color as keyof typeof COLOR_CLASS]} shadow-md`
          }`}
        >
          <Icon className="w-5 h-5 text-white" />
        </div>
        <h3 className="text-[14px] font-bold text-foreground mb-1.5">
          {title}
        </h3>
        <p className="text-[12px] text-muted-foreground leading-relaxed line-clamp-2 mb-3">{desc}</p>
        {(chartCount !== undefined && fieldCount !== undefined) && (
          <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1">
              <BarChart3 className="w-3 h-3" />
              {chartCount} 图表
            </span>
            <span>{fieldCount} 字段</span>
          </div>
        )}
        {/* 底部箭头 */}
        <div className="mt-3 flex items-center text-[11px] text-primary font-medium opacity-0 group-hover:opacity-100 transition-opacity">
          使用模板 <ArrowRight className="w-3 h-3 ml-1" />
        </div>
      </div>
    </button>
  );
}

function DefaultTemplateCard({
  tpl,
  onClick,
}: {
  tpl: DefaultTemplate;
  onClick: () => void;
}) {
  const Icon = ICON_MAP[tpl.iconName];
  const chartCount = tpl.blocks.filter((b) => b.type === "chart").length;
  return (
    <button
      onClick={onClick}
      className="group text-left bg-white rounded-xl border border-border-light/80 hover:border-primary/30 hover:shadow-lg hover:-translate-y-1 transition-all duration-300 relative overflow-hidden"
    >
      {/* 顶部渐变条 */}
      <div className={`h-1.5 bg-gradient-to-r ${COLOR_CLASS[tpl.color]}`} />

      <div className="p-5">
        <div className="absolute top-3 right-3 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-muted text-muted-foreground">
          系统
        </div>
        <div className={`w-11 h-11 rounded-xl flex items-center justify-center mb-3.5 bg-gradient-to-br ${COLOR_CLASS[tpl.color]} shadow-md`}>
          <Icon className="w-5 h-5 text-white" />
        </div>
        <h3 className="text-[14px] font-bold text-foreground mb-1.5">
          {tpl.title}
        </h3>
        <p className="text-[12px] text-muted-foreground leading-relaxed line-clamp-2 mb-3">{tpl.desc}</p>
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
          <span className="px-2 py-0.5 rounded-md bg-muted/80 font-medium">{tpl.category}</span>
          <span className="flex items-center gap-1">
            <BarChart3 className="w-3 h-3" />
            {chartCount} 图表
          </span>
          <span>{tpl.suggestedFields.length} 字段</span>
        </div>
        {/* 底部箭头 */}
        <div className="mt-3 flex items-center text-[11px] text-primary font-medium opacity-0 group-hover:opacity-100 transition-opacity">
          使用模板 <ArrowRight className="w-3 h-3 ml-1" />
        </div>
      </div>
    </button>
  );
}
