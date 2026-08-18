import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Mail, AlertCircle, CheckCircle, Loader2, ArrowLeft } from "lucide-react";

export default function ForgotPassword() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!email) {
      setError("请输入邮箱地址");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const response = await fetch("http://localhost:8000/api/v1/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!response.ok) {
        throw new Error("请求失败");
      }
      setSent(true);
    } catch {
      setError("请求失败，请稍后重试");
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
            忘记密码？
            <br />
            没关系
          </h1>
          <p className="text-base leading-relaxed max-w-md text-muted-foreground">
            输入你的注册邮箱，我们会发送重置链接
          </p>
        </div>

        <div />
      </section>

      <section className="w-[55%] flex items-center justify-center px-16 bg-card">
        <div className="w-full max-w-md space-y-8">
          <div className="space-y-2">
            <h2 className="text-2xl font-bold tracking-tight text-foreground">重置密码</h2>
            <p className="text-sm text-muted-foreground">
              {sent ? "请查收邮件" : "输入你的注册邮箱地址"}
            </p>
          </div>

          {sent ? (
            <div className="space-y-5">
              <div className="flex flex-col items-center gap-3 py-6 text-center">
                <div className="w-14 h-14 rounded-full bg-green-100 flex items-center justify-center">
                  <CheckCircle className="w-8 h-8 text-green-600" />
                </div>
                <div>
                  <p className="font-medium text-foreground">邮件已发送</p>
                  <p className="text-sm text-muted-foreground mt-1">
                    我们已将密码重置链接发送到 <strong>{email}</strong>
                  </p>
                  <p className="text-sm text-muted-foreground mt-1">
                    请查收邮件并按照指引重置密码
                  </p>
                </div>
              </div>
              <button
                onClick={() => navigate("/login")}
                className="w-full py-2.5 rounded-lg text-sm font-semibold bg-primary text-primary-foreground hover:bg-primary-hover transition-all duration-200"
              >
                返回登录
              </button>
            </div>
          ) : (
            <form className="space-y-5" onSubmit={handleSubmit}>
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
                {loading ? "发送中..." : "发送重置链接"}
              </button>
            </form>
          )}

          <p className="text-center text-sm text-muted-foreground">
            想起密码了？
            <a href="/login" className="font-semibold text-primary hover:text-primary-hover">
              返回登录
            </a>
          </p>
        </div>
      </section>
    </main>
  );
}
