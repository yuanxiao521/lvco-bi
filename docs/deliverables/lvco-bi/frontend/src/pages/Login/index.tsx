import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  Mail,
  Lock,
  Eye,
  EyeOff,
  Smartphone,
  ShieldCheck,
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
  const [tab, setTab] = useState<"account" | "phone">("account");
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
      const message =
        (err as { response?: { data?: { error?: { message?: string } } }; message?: string })
          ?.response?.data?.error?.message ||
        (err as { message?: string })?.message ||
        "登录失败，请检查邮箱或密码";
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
            {/* TODO: 从API获取 */}
            智能数据分析
            <br />
            从此开始
          </h1>
          <p className="text-base leading-relaxed max-w-md text-muted-foreground">
            {/* TODO: 从API获取 */}
            AI 驱动的 BI 平台，让你的数据洞察更快、更准确
          </p>
        </div>

        <div className="relative z-10 flex gap-3">
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <LayoutDashboard className="w-4 h-4 text-primary" />
            {/* TODO: 从API获取 */}
            自由画布
          </div>
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <Sparkles className="w-4 h-4 text-ai" />
            {/* TODO: 从API获取 */}
            AI 分析
          </div>
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-white/85 shadow-sm text-card-foreground">
            <Users className="w-4 h-4 text-primary" />
            {/* TODO: 从API获取 */}
            实时协作
          </div>
        </div>
      </section>

      <section className="w-[55%] flex items-center justify-center px-16 bg-card">
        <div className="w-full max-w-md space-y-8">
          <div className="space-y-2">
            <h2 className="text-2xl font-bold tracking-tight text-foreground">
              {/* TODO: 从API获取 */}
              欢迎回来
            </h2>
            <p className="text-sm text-muted-foreground">
              {/* TODO: 从API获取 */}
              登录你的 Lvco BI 账户
            </p>
          </div>

          <div className="flex rounded-lg p-1 bg-muted">
            <button
              className={`flex-1 py-2 text-sm font-medium rounded-md transition-all duration-200 ${
                tab === "account" ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"
              }`}
              onClick={() => setTab("account")}
            >
              {/* TODO: 从API获取 */}
              账号密码
            </button>
            <button
              className={`flex-1 py-2 text-sm font-medium rounded-md transition-all duration-200 ${
                tab === "phone" ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"
              }`}
              onClick={() => setTab("phone")}
            >
              {/* TODO: 从API获取 */}
              手机验证码
            </button>
          </div>

          {tab === "account" && (
            <form className="space-y-5" onSubmit={handleAccountSubmit}>
              <div className="space-y-2">
                <label className="text-sm font-medium text-card-foreground">
                  {/* TODO: 从API获取 */}
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
                  {/* TODO: 从API获取 */}
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
                    {/* TODO: 从API获取 */}
                    记住我
                  </span>
                </label>
                <a href="/forgot-password" className="text-sm font-medium text-primary hover:text-primary-hover">
                  {/* TODO: 从API获取 */}
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
                {/* TODO: 从API获取 */}
                {loading ? "登录中..." : "登 录"}
              </button>
            </form>
          )}

          {tab === "phone" && (
            <form className="space-y-5" onSubmit={(e) => e.preventDefault()}>
              <div className="space-y-2">
                <label className="text-sm font-medium text-card-foreground">
                  {/* TODO: 从API获取 */}
                  手机号
                </label>
                <div className="relative">
                  <Smartphone className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <input
                    type="tel"
                    placeholder="请输入手机号"
                    className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)]"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-card-foreground">
                  {/* TODO: 从API获取 */}
                  验证码
                </label>
                <div className="flex gap-3">
                  <div className="relative flex-1">
                    <ShieldCheck className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                    <input
                      type="text"
                      placeholder="输入验证码"
                      maxLength={6}
                      className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg border border-border bg-card text-foreground outline-none transition-all duration-200 focus:border-ring focus:shadow-[0_0_0_3px_rgba(43,181,160,0.1)]"
                    />
                  </div>
                  <button
                    type="button"
                    className="px-4 py-2.5 text-sm font-medium rounded-lg border border-primary text-primary bg-transparent hover:bg-primary-light transition-all duration-200"
                  >
                    {/* TODO: 从API获取 */}
                    获取验证码
                  </button>
                </div>
              </div>

              <button
                type="submit"
                className="w-full py-2.5 rounded-lg text-sm font-semibold bg-primary text-primary-foreground hover:bg-primary-hover transition-all duration-200"
              >
                {/* TODO: 从API获取 */}
                登 录
              </button>
            </form>
          )}

          <div className="relative flex items-center">
            <div className="flex-1 h-px bg-border" />
            <span className="px-4 text-xs text-muted-foreground">
              {/* TODO: 从API获取 */}
              或
            </span>
            <div className="flex-1 h-px bg-border" />
          </div>

          <div className="flex gap-3">
            <button className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-lg border border-border text-sm font-medium text-card-foreground bg-card hover:bg-muted transition-all duration-200">
              <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none">
                <path
                  d="M8.69 13.75c-.18-.31-.09-.62.13-.97l.01-.02c.22-.35.46-.7.28-1.01-.18-.31-.62-.31-1.06-.31s-.88 0-1.06.31c-.18.31.06.66.28 1.01l.01.02c.22.35.31.66.13.97-.18.31-.62.31-1.06.31-.3 0-.6 0-.82-.12a1.6 1.6 0 0 1-.5-.5 4.7 4.7 0 0 1-.56-1.19A6.4 6.4 0 0 1 4 10c0-3.31 2.69-6 6-6s6 2.69 6 6a6 6 0 0 1-.44 2.25 4.6 4.6 0 0 1-.56 1.19 1.6 1.6 0 0 1-.5.5c-.22.12-.52.12-.82.12-.44 0-.88 0-1.06-.31Z"
                  fill="#07C160"
                />
                <path
                  d="M15.31 17.53c-.2-.35-.1-.72.15-1.1l.01-.02c.25-.38.52-.76.32-1.11-.2-.35-.7-.35-1.2-.35s-1 0-1.2.35c-.2.35.07.73.32 1.11l.01.02c.25.38.35.75.15 1.1-.2.35-.7.35-1.2.35-.34 0-.68 0-.93-.14a1.8 1.8 0 0 1-.57-.56 5.2 5.2 0 0 1-.63-1.33A7.2 7.2 0 0 1 10 14c0-3.7 2.8-6.7 6.25-6.7S22.5 10.3 22.5 14a7.2 7.2 0 0 1-.5 2.55 5.2 5.2 0 0 1-.63 1.33 1.8 1.8 0 0 1-.57.56c-.25.14-.59.14-.93.14-.5 0-1 0-1.2-.35Z"
                  fill="#07C160"
                />
              </svg>
              {/* TODO: 从API获取 */}
              微信登录
            </button>
            <button className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-lg border border-border text-sm font-medium text-card-foreground bg-card hover:bg-muted transition-all duration-200">
              <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none">
                <path
                  d="M12 2C6.48 2 2 5.82 2 10.5c0 2.4 1.23 4.56 3.2 6.08-.16.98-.6 3.12-.68 3.56 0 0-.01.14.07.2.08.05.18.03.18.03.53-.07 3.06-1.34 3.82-1.75.78.18 1.6.28 2.41.28 5.52 0 10-3.82 10-8.5S17.52 2 12 2Z"
                  fill="#0089FF"
                />
                <circle cx="8.5" cy="10.5" r="1.2" fill="white" />
                <circle cx="12" cy="10.5" r="1.2" fill="white" />
                <circle cx="15.5" cy="10.5" r="1.2" fill="white" />
              </svg>
              {/* TODO: 从API获取 */}
              钉钉登录
            </button>
          </div>

          <p className="text-center text-sm text-muted-foreground">
            {/* TODO: 从API获取 */}
            还没有账户？
            <a href="/register" className="font-semibold text-primary hover:text-primary-hover">
              {/* TODO: 从API获取 */}
              立即注册
            </a>
          </p>
        </div>
      </section>
    </main>
  );
}
