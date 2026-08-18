import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  Mail,
  Lock,
  Eye,
  EyeOff,
  LayoutDashboard,
  Sparkles,
  Users,
  AlertCircle,
  Loader2,
} from "lucide-react";
import { login } from "../../api/auth";
import { tokenStore } from "../../api/client";
import { useAuthStore } from "../../stores/authStore";

export default function Login() {
  const navigate = useNavigate();
  const [showPassword, setShowPassword] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleAccountSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!email || !password) {
      setError("请输入邮箱和密码");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const result = await login({ email, password });
      tokenStore.set(result.accessToken, result.refreshToken ?? "");
      tokenStore.setUser(result.user);
      useAuthStore.getState().setUser(result.user);
      navigate("/", { replace: true });
    } catch (err) {
      const axiosErr = err as {
        response?: {
          data?: {
            detail?: { code?: string; message?: string };
            message?: string;
          };
          error?: { message?: string };
        };
        message?: string;
      };
      const detail = axiosErr?.response?.data?.detail;
      let message: string;

      if (detail?.message) {
        message = detail.message;
      } else if (axiosErr?.response?.data?.error?.message) {
        message = axiosErr.response.data.error.message;
      } else if (axiosErr?.message) {
        message = axiosErr.message;
      } else {
        message = "登录失败，请稍后重试";
      }

      setError(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="h-screen w-screen flex overflow-hidden bg-background">
      <section
        className="relative w-[45%] flex flex-col justify-between px-14 py-16 overflow-hidden"
        style={{ background: "linear-gradient(160deg, #E8F7F4 0%, #EEF0FE 100%)" }}
      >
        <div
          className="absolute -top-20 -left-20 w-72 h-72 rounded-full opacity-30"
          style={{ background: "radial-gradient(circle, #B8E8E0 0%, transparent 70%)" }}
        />
        <div
          className="absolute -bottom-16 -right-16 w-80 h-80 rounded-full opacity-20"
          style={{ background: "radial-gradient(circle, #C7CCF9 0%, transparent 70%)" }}
        />

        <div className="relative z-10 flex items-center gap-3">
          <div className="w-12 h-12 rounded-xl flex items-center justify-center bg-primary-light">
            <svg viewBox="0 0 80 80" className="w-8 h-8">
              <rect x="8" y="12" width="6" height="56" rx="2" fill="#2BB5A0" />
              <rect x="14" y="64" width="58" height="4" rx="1" fill="#2BB5A0" />
              <rect x="14" y="20" width="4" height="44" rx="1" fill="#2BB5A0" />
              <rect x="68" y="20" width="4" height="44" rx="1" fill="#2BB5A0" />
              <path
                d="M18 20 C18 8, 30 4, 42 4 C54 4, 66 8, 66 20"
                stroke="#2BB5A0"
                strokeWidth="4"
                fill="none"
                strokeLinecap="round"
              />
              <rect x="22" y="52" width="4" height="12" rx="1" fill="white" opacity="0.9" />
              <rect x="28" y="46" width="4" height="18" rx="1" fill="white" opacity="0.75" />
              <rect x="34" y="40" width="4" height="24" rx="1" fill="white" />
            </svg>
          </div>
          <span className="text-xl font-bold tracking-tight text-foreground">Lvco BI</span>
        </div>

        <div className="relative z-10 space-y-5">
          <h1 className="text-4xl font-extrabold leading-tight tracking-tight text-foreground">
            智能数据分析
            <br />
            从此开始
          </h1>
          <p className="text-base leading-relaxed max-w-md text-muted-foreground">
            AI 驱动的 BI 平台，让你的数据洞察更快、更准确
          </p>
        </div>

        <div className="relative z-10 flex gap-3">
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <LayoutDashboard className="w-4 h-4 text-primary" />
            自由画布
          </div>
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <Sparkles className="w-4 h-4 text-ai" />
            AI 分析
          </div>
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <Users className="w-4 h-4 text-primary" />
            实时协作
          </div>
        </div>
      </section>

      <section className="w-[55%] flex items-center justify-center px-16 bg-card">
        <div className="w-full max-w-md space-y-8">
          <div className="space-y-2">
            <h2 className="text-2xl font-bold tracking-tight text-foreground">
              欢迎回来
            </h2>
            <p className="text-sm text-muted-foreground">
              登录你的 Lvco BI 账户
            </p>
          </div>

          <form className="space-y-5" onSubmit={handleAccountSubmit}>
            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">
                邮箱地址
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="email"
                  placeholder="name@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  disabled={loading}
                  autoComplete="email"
                  className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)] disabled:opacity-60 disabled:cursor-not-allowed"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">
                密码
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type={showPassword ? "text" : "password"}
                  placeholder="输入密码"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={loading}
                  autoComplete="current-password"
                  className="w-full pl-10 pr-10 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)] disabled:opacity-60 disabled:cursor-not-allowed"
                />
                <button
                  type="button"
                  className="absolute right-3.5 top-1/2 -translate-y-1/2"
                  onClick={() => setShowPassword(!showPassword)}
                  tabIndex={-1}
                >
                  {showPassword ? (
                    <Eye className="w-4 h-4 text-muted-foreground" />
                  ) : (
                    <EyeOff className="w-4 h-4 text-muted-foreground" />
                  )}
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between">
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  className="w-4 h-4 rounded border accent-primary"
                />
                <span className="text-sm text-muted-foreground">
                  记住我
                </span>
              </label>
              <a href="/forgot-password" className="text-sm font-medium text-primary hover:text-primary-hover">
                忘记密码？
              </a>
            </div>

            {error && (
              <div className="flex items-start gap-2 px-3 py-2.5 rounded-lg border border-red-200 bg-red-50 text-sm text-red-700">
                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 rounded-lg text-sm font-semibold bg-primary text-primary-foreground hover:bg-primary-hover transition-all duration-200 disabled:opacity-60 disabled:cursor-not-allowed flex items-center justify-center gap-2"
            >
              {loading && <Loader2 className="w-4 h-4 animate-spin" />}
              {loading ? "登录中..." : "登 录"}
            </button>
          </form>

          <p className="text-center text-sm text-muted-foreground">
            还没有账户？
            <a href="/register" className="font-semibold text-primary hover:text-primary-hover">
              立即注册
            </a>
          </p>
        </div>
      </section>
    </main>
  );
}
