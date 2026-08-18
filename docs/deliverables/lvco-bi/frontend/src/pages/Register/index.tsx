import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  Mail,
  Lock,
  Eye,
  EyeOff,
  User,
  AlertCircle,
  Loader2,
  ArrowLeft,
} from "lucide-react";
import { register } from "../../api/auth";
import { tokenStore } from "../../api/client";
import { useAuthStore } from "../../stores/authStore";

export default function Register() {
  const navigate = useNavigate();
  const [showPassword, setShowPassword] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!email || !password || !displayName) {
      setError("请填写所有字段");
      return;
    }
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const result = await register({ email, password, displayName });
      tokenStore.set(result.accessToken, result.refreshToken ?? "");

      tokenStore.setUser(result.user);
      useAuthStore.getState().setUser(result.user);
      navigate("/", { replace: true });
    } catch (err) {
      const message =
        (err as { response?: { data?: { error?: { message?: string } } }; message?: string })
          ?.response?.data?.error?.message ||
        (err as { message?: string })?.message ||
        "注册失败，请稍后重试";
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
          <button
            onClick={() => navigate("/login")}
            className="w-10 h-10 rounded-xl flex items-center justify-center bg-white/80 shadow-sm hover:bg-white transition-colors"
          >
            <ArrowLeft className="w-5 h-5 text-foreground" />
          </button>
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
            创建你的
            <br />
            Lvco BI 账户
          </h1>
          <p className="text-base leading-relaxed max-w-md text-muted-foreground">
            几步完成注册，开启智能数据分析之旅
          </p>
        </div>

        <div />
      </section>

      <section className="w-[55%] flex items-center justify-center px-16 bg-card">
        <div className="w-full max-w-md space-y-8">
          <div className="space-y-2">
            <h2 className="text-2xl font-bold tracking-tight text-foreground">注册账户</h2>
            <p className="text-sm text-muted-foreground">填写以下信息完成注册</p>
          </div>

          <form className="space-y-5" onSubmit={handleSubmit}>
            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">昵称</label>
              <div className="relative">
                <User className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="text"
                  placeholder="你的昵称"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  disabled={loading}
                  autoComplete="name"
                  className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)] disabled:opacity-60"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">邮箱地址</label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="email"
                  placeholder="name@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  disabled={loading}
                  autoComplete="email"
                  className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)] disabled:opacity-60"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">密码</label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type={showPassword ? "text" : "password"}
                  placeholder="至少 8 位"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={loading}
                  autoComplete="new-password"
                  className="w-full pl-10 pr-10 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)] disabled:opacity-60"
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
              {loading ? "注册中..." : "注册"}
            </button>
          </form>

          <p className="text-center text-sm text-muted-foreground">
            已有账户？
            <a href="/login" className="font-semibold text-primary hover:text-primary-hover">
              立即登录
            </a>
          </p>
        </div>
      </section>
    </main>
  );
}
