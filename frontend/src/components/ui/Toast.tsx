import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";
import { X, CheckCircle2, AlertTriangle, Info, AlertCircle } from "lucide-react";

type ToastType = "info" | "success" | "error" | "warning";

interface Toast {
  id: number;
  type: ToastType;
  message: string;
}

interface ToastCtx {
  show: (type: ToastType, message: string) => void;
  info: (message: string) => void;
  success: (message: string) => void;
  error: (message: string) => void;
  warning: (message: string) => void;
  confirm: (message: string) => Promise<boolean>;
}

const ToastContext = createContext<ToastCtx | null>(null);

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

const iconMap: Record<ToastType, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  error: AlertCircle,
  warning: AlertTriangle,
};

const bgMap: Record<ToastType, string> = {
  info: "bg-[#EFF6FF] border-info text-info",
  success: "bg-success-light border-success text-success",
  error: "bg-danger-light border-danger text-danger",
  warning: "bg-[#FFFBEB] border-[#F59E0B] text-[#92400E]",
};

const AUTO_DISMISS_MS = 3500;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const confirmResolve = useRef<((val: boolean) => void) | null>(null);
  const [confirmMsg, setConfirmMsg] = useState<string | null>(null);

  const show = useCallback((type: ToastType, message: string) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, AUTO_DISMISS_MS);
  }, []);

  const info = useCallback((m: string) => show("info", m), [show]);
  const success = useCallback((m: string) => show("success", m), [show]);
  const error = useCallback((m: string) => show("error", m), [show]);
  const warning = useCallback((m: string) => show("warning", m), [show]);

  const confirm = useCallback(
    (message: string): Promise<boolean> =>
      new Promise((resolve) => {
        confirmResolve.current = resolve;
        setConfirmMsg(message);
      }),
    []
  );

  const handleConfirm = (val: boolean) => {
    confirmResolve.current?.(val);
    confirmResolve.current = null;
    setConfirmMsg(null);
  };

  return (
    <ToastContext.Provider value={{ show, info, success, error, warning, confirm }}>
      {children}

      {/* Toast 浮层 */}
      <div className="fixed top-5 right-5 z-[100] flex flex-col gap-2 pointer-events-none">
        {toasts.map((t) => {
          const Icon = iconMap[t.type];
          return (
            <div
              key={t.id}
              className={`pointer-events-auto flex items-start gap-2 px-4 py-3 rounded-[10px] border shadow-lg min-w-[280px] max-w-[420px] animate-slide-in ${bgMap[t.type]}`}
              style={{
                animation: "slideInRight 0.25s ease-out",
              }}
            >
              <Icon className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span className="text-[13px] leading-relaxed flex-1">{t.message}</span>
              <button
                onClick={() =>
                  setToasts((prev) => prev.filter((p) => p.id !== t.id))
                }
                className="p-0.5 -mr-1 flex-shrink-0 opacity-60 hover:opacity-100"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          );
        })}
      </div>

      {/* Confirm 弹窗 */}
      {confirmMsg != null ? (
        <div className="fixed inset-0 z-[110] flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => handleConfirm(false)}
          />
          <div className="relative bg-white rounded-lg shadow-xl w-[min(380px,92vw)] p-5">
            <div className="flex items-start gap-3 mb-4">
              <AlertTriangle className="w-5 h-5 text-[#F59E0B] flex-shrink-0 mt-0.5" />
              <p className="text-[14px] text-card-foreground leading-relaxed">
                {confirmMsg}
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => handleConfirm(false)}
                className="px-4 py-2 rounded-[8px] text-[13px] font-medium border border-border bg-white text-card-foreground hover:bg-muted transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => handleConfirm(true)}
                className="px-4 py-2 rounded-[8px] text-[13px] font-medium bg-danger text-white hover:bg-danger/90 transition-colors"
              >
                确认
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <style>{`
        @keyframes slideInRight {
          from { opacity: 0; transform: translateX(40px); }
          to { opacity: 1; transform: translateX(0); }
        }
      `}</style>
    </ToastContext.Provider>
  );
}
