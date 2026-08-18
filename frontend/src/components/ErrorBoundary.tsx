import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 渲染失败时显示的位置：page=整页, panel=面板内（默认 panel） */
  scope?: "page" | "panel";
  /** 失败兜底文案 */
  fallbackTitle?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

/**
 * 全局错误边界：捕获任何子组件渲染时的运行时错误，避免整页白屏。
 * 屏幕上展示错误堆栈概要，控制台输出完整 stack 方便排查。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null, errorInfo: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, errorInfo);
    this.setState({ errorInfo });
  }

  private handleReset = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render() {
    if (!this.state.hasError) return this.props.children;

    const title = this.props.fallbackTitle || "页面渲染失败";
    const msg = this.state.error?.message || "未知错误";
    const stack = this.state.error?.stack || "";
    const componentStack = this.state.errorInfo?.componentStack || "";

    return (
      <div
        className={
          this.props.scope === "page"
            ? "flex-1 flex items-center justify-center p-8 bg-background"
            : "m-4 p-5 rounded-lg border border-danger/40 bg-danger-light/40"
        }
      >
        <div className="max-w-[760px] w-full">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-2 h-2 rounded-full bg-danger animate-pulse" />
            <h2 className="text-[15px] font-semibold text-danger">{title}</h2>
          </div>
          <p className="text-[13px] text-foreground mb-3 break-all">
            <b>错误：</b>
            {msg}
          </p>
          {stack ? (
            <details className="mb-2">
              <summary className="cursor-pointer text-[12px] text-muted-foreground">
                错误堆栈
              </summary>
              <pre className="mt-2 text-[11px] bg-muted/40 p-3 rounded overflow-auto max-h-[240px] whitespace-pre-wrap break-all">
                {stack}
              </pre>
            </details>
          ) : null}
          {componentStack ? (
            <details className="mb-3">
              <summary className="cursor-pointer text-[12px] text-muted-foreground">
                组件堆栈
              </summary>
              <pre className="mt-2 text-[11px] bg-muted/40 p-3 rounded overflow-auto max-h-[240px] whitespace-pre-wrap break-all">
                {componentStack}
              </pre>
            </details>
          ) : null}
          <div className="flex gap-2">
            <button
              onClick={this.handleReset}
              className="px-3 py-1.5 text-[12px] rounded-md bg-primary text-white hover:bg-primary-hover transition-colors"
            >
              重试
            </button>
            <button
              onClick={() => window.location.reload()}
              className="px-3 py-1.5 text-[12px] rounded-md border border-border hover:bg-muted transition-colors"
            >
              刷新页面
            </button>
          </div>
        </div>
      </div>
    );
  }
}