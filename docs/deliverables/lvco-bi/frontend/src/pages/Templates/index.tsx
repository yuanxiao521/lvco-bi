import { useNavigate } from "react-router-dom";
import {
  LayoutTemplate,
  Sparkles,
  FileText,
  TrendingUp,
  Users,
  Activity,
  Wallet,
} from "lucide-react";
import { DEFAULT_TEMPLATES, type DefaultTemplate } from "../../data/defaultTemplates";

const ICON_MAP = { TrendingUp, Users, Activity, Wallet } as const;
const COLOR_CLASS = {
  primary: "bg-primary-light text-primary",
  info: "bg-info-light text-info",
  success: "bg-success-light text-success",
  warning: "bg-warning-light text-warning",
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
    <div className="flex-1 p-6 space-y-6">
      {/* 页头 */}
      <div>
        <h1 className="text-[17px] font-semibold text-foreground flex items-center gap-2">
          <LayoutTemplate className="w-5 h-5 text-primary" />
          模板库
        </h1>
        <p className="text-[12px] text-muted-foreground mt-1">
          基于系统模板或已有画布快速复用，避免从零开始
        </p>
      </div>

      {/* 默认模板 */}
      <Section
        icon={Sparkles}
        title="默认模板"
        desc="由系统提供，开箱即用。选择数据源后即可生成图表"
      >
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {/* 空白模板置顶 */}
          <TemplateCard
            title="空白模板"
            desc="从零开始，构建你的第一份分析报告"
            icon={FileText}
            color="default"
            onClick={() => handleSelect("system-blank")}
            tag="推荐起步"
          />
          {DEFAULT_TEMPLATES.map((tpl) => (
            <DefaultTemplateCard
              key={tpl.id}
              tpl={tpl}
              onClick={() => handleSelect(tpl.id)}
            />
          ))}
        </div>
      </Section>
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  desc,
  children,
}: {
  icon: typeof Sparkles;
  title: string;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="flex items-start gap-2 mb-3">
        <div className="w-7 h-7 rounded-lg bg-primary-light text-primary flex items-center justify-center flex-shrink-0">
          <Icon className="w-3.5 h-3.5" />
        </div>
        <div>
          <h2 className="text-[14px] font-semibold text-foreground">{title}</h2>
          <p className="text-[11px] text-muted-foreground mt-0.5">{desc}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function TemplateCard({
  title,
  desc,
  icon: Icon,
  color,
  onClick,
  tag,
}: {
  title: string;
  desc: string;
  icon: typeof FileText;
  color: keyof typeof COLOR_CLASS | "default";
  onClick: () => void;
  tag?: string;
}) {
  return (
    <button
      onClick={onClick}
      className="group text-left bg-white rounded-[10px] border border-border-light p-4 hover:shadow-card hover:-translate-y-0.5 transition-all relative w-full"
    >
      {tag && (
        <span className="absolute top-2 right-2 px-1.5 py-0.5 rounded text-[10px] font-medium bg-primary-light text-primary">
          {tag}
        </span>
      )}
      <div
        className={`w-10 h-10 rounded-lg flex items-center justify-center mb-3 ${
          color === "default" ? "bg-primary text-white" : COLOR_CLASS[color]
        }`}
      >
        <Icon className="w-5 h-5" />
      </div>
      <h3 className="text-[13px] font-semibold text-foreground mb-1 truncate">
        {title}
      </h3>
      <p className="text-[11px] text-muted-foreground line-clamp-2">{desc}</p>
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
      className="group text-left bg-white rounded-[10px] border border-border-light p-4 hover:shadow-card hover:-translate-y-0.5 transition-all relative w-full"
    >
      <div className="absolute top-2 right-2 px-1.5 py-0.5 rounded text-[10px] font-medium bg-ai-light text-ai">
        系统
      </div>
      <div className={`w-10 h-10 rounded-lg flex items-center justify-center mb-3 ${COLOR_CLASS[tpl.color]}`}>
        <Icon className="w-5 h-5" />
      </div>
      <h3 className="text-[13px] font-semibold text-foreground mb-1 truncate">
        {tpl.title}
      </h3>
      <p className="text-[11px] text-muted-foreground line-clamp-2 mb-2">{tpl.desc}</p>
      <div className="text-[10px] text-muted-foreground flex items-center gap-2">
        <span className="px-1.5 py-0.5 rounded bg-muted">{tpl.category}</span>
        <span>{chartCount} 图表 · {tpl.suggestedFields.length} 字段</span>
      </div>
    </button>
  );
}

