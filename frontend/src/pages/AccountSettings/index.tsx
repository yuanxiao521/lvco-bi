import { useEffect, useState } from 'react'
import { useAuthStore } from '../../stores/authStore'
import { changePassword, updateProfile } from '../../api/auth'
import {
  Loader2,
  User as UserIcon,
  Lock,
  Sparkles,
  Check,
} from 'lucide-react'

type TabKey = 'profile' | 'password' | 'ai'

interface AiPreferences {
  defaultModel: string
  temperature: number
  streamResponse: boolean
  showRationale: boolean
  customPromptHint: string
}

const AI_PREFS_KEY = 'lvco_ai_preferences'

const DEFAULT_AI: AiPreferences = {
  defaultModel: 'deepseek-chat',
  temperature: 0.5,
  streamResponse: true,
  showRationale: true,
  customPromptHint: '',
}

export default function AccountSettingsPage() {
  const [tab, setTab] = useState<TabKey>('profile')
  const { user, setUser } = useAuthStore()

  // Profile state
  const [displayName, setDisplayName] = useState(user?.displayName || '')
  const [email, setEmail] = useState(user?.email || '')
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileMsg, setProfileMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  // Password state
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordLoading, setPasswordLoading] = useState(false)
  const [passwordMsg, setPasswordMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  // AI preferences state (localStorage)
  const [aiPrefs, setAiPrefs] = useState<AiPreferences>(DEFAULT_AI)
  const [aiMsg, setAiMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  // 加载本地偏好
  useEffect(() => {
    try {
      const rawAi = localStorage.getItem(AI_PREFS_KEY)
      if (rawAi) setAiPrefs({ ...DEFAULT_AI, ...JSON.parse(rawAi) })
    } catch {
      // 静默
    }
  }, [])

  const handleProfileSave = async () => {
    setProfileLoading(true)
    setProfileMsg(null)
    try {
      const updated = await updateProfile({ display_name: displayName, email })
      setUser(updated)
      setProfileMsg({ type: 'success', text: '个人资料已更新' })
    } catch (err: any) {
      setProfileMsg({ type: 'error', text: err?.response?.data?.detail?.message || '更新失败' })
    } finally {
      setProfileLoading(false)
    }
  }

  const handlePasswordSave = async () => {
    if (newPassword !== confirmPassword) {
      setPasswordMsg({ type: 'error', text: '两次密码不一致' })
      return
    }
    setPasswordLoading(true)
    setPasswordMsg(null)
    try {
      await changePassword(oldPassword, newPassword)
      setPasswordMsg({ type: 'success', text: '密码修改成功' })
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
    } catch (err: any) {
      setPasswordMsg({ type: 'error', text: err?.response?.data?.detail?.message || '修改失败' })
    } finally {
      setPasswordLoading(false)
    }
  }

  const handleAiSave = () => {
    try {
      localStorage.setItem(AI_PREFS_KEY, JSON.stringify(aiPrefs))
      setAiMsg({ type: 'success', text: 'AI 设置已保存' })
    } catch {
      setAiMsg({ type: 'error', text: '保存失败：浏览器存储不可用' })
    }
  }

  return (
    <div className="flex-1 p-6 space-y-5">
      <h1 className="text-[17px] font-semibold text-foreground">账号设置</h1>

      {/* Tabs */}
      <div className="border-b border-border-light flex gap-0 -mb-px">
        <TabButton active={tab === 'profile'} onClick={() => setTab('profile')} icon={UserIcon} label="个人资料" />
        <TabButton active={tab === 'password'} onClick={() => setTab('password')} icon={Lock} label="修改密码" />
        <TabButton active={tab === 'ai'} onClick={() => setTab('ai')} icon={Sparkles} label="AI 设置" />
      </div>

      {tab === 'profile' && (
        <div className="bg-white rounded-md shadow-card border border-border p-6">
          <h2 className="text-lg font-medium mb-4">个人资料</h2>
          <div className="space-y-4 max-w-md">
            <Field label="显示名称">
              <input value={displayName} onChange={e => setDisplayName(e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm" />
            </Field>
            <Field label="邮箱">
              <input value={email} onChange={e => setEmail(e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm" />
            </Field>
            <Field label="用户角色">
              <div className="px-3 py-2 border border-border rounded-md bg-muted text-sm text-muted-foreground">
                {user?.role || '—'}（角色由管理员分配，不可自行修改）
              </div>
            </Field>
            <StatusMsg msg={profileMsg} />
            <SaveButton loading={profileLoading} onClick={handleProfileSave} label="保存" />
          </div>
        </div>
      )}

      {tab === 'password' && (
        <div className="bg-white rounded-md shadow-card border border-border p-6">
          <h2 className="text-lg font-medium mb-4">修改密码</h2>
          <div className="space-y-4 max-w-md">
            <Field label="旧密码">
              <input type="password" value={oldPassword} onChange={e => setOldPassword(e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm" />
            </Field>
            <Field label="新密码">
              <input type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm" />
              <p className="text-[11px] text-muted-foreground mt-1">至少 8 位，建议包含大小写字母与数字</p>
            </Field>
            <Field label="确认新密码">
              <input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm" />
            </Field>
            <StatusMsg msg={passwordMsg} />
            <SaveButton loading={passwordLoading} onClick={handlePasswordSave} label="修改密码" />
          </div>
        </div>
      )}

      {tab === 'ai' && (
        <div className="bg-white rounded-md shadow-card border border-border p-6">
          <h2 className="text-lg font-medium mb-1">AI 设置</h2>
          <p className="text-[12px] text-muted-foreground mb-4">
            影响 AI 助手 / 推荐 / 洞察 / 润色 行为的个性化选项
          </p>
          <div className="space-y-4 max-w-md">
            <Field label="默认模型">
              <select
                value={aiPrefs.defaultModel}
                onChange={e => setAiPrefs({ ...aiPrefs, defaultModel: e.target.value })}
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm"
              >
                <option value="deepseek-chat">DeepSeek Chat（默认）</option>
                <option value="gpt-4o">GPT-4o</option>
                <option value="qwen-plus">通义千问 Plus</option>
                <option value="glm-4-plus">智谱 GLM-4 Plus</option>
              </select>
              <p className="text-[11px] text-muted-foreground mt-1">
                模型由后端 OPENAI_BASE_URL 路由，本设置仅控制默认选择
              </p>
            </Field>
            <Field label={`温度 (Temperature): ${aiPrefs.temperature.toFixed(2)}`}>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={aiPrefs.temperature}
                onChange={e => setAiPrefs({ ...aiPrefs, temperature: Number(e.target.value) })}
                className="w-full"
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                0 = 精确稳定，1 = 创造性高。数据洞察建议 0.3，文案润色建议 0.7
              </p>
            </Field>
            <Field label="响应方式">
              <div className="space-y-2">
                <ToggleRow
                  label="流式输出（SSE）"
                  desc="实时逐字显示，体验更流畅"
                  checked={aiPrefs.streamResponse}
                  onChange={v => setAiPrefs({ ...aiPrefs, streamResponse: v })}
                />
                <ToggleRow
                  label="显示 AI 推荐理由"
                  desc="在 AI 图表推荐中展示选择依据"
                  checked={aiPrefs.showRationale}
                  onChange={v => setAiPrefs({ ...aiPrefs, showRationale: v })}
                />
              </div>
            </Field>
            <Field label="自定义提示词补充">
              <textarea
                value={aiPrefs.customPromptHint}
                onChange={e => setAiPrefs({ ...aiPrefs, customPromptHint: e.target.value })}
                rows={3}
                placeholder="例如：请使用中文输出 / 偏向金融行业术语"
                className="w-full px-3 py-2 border border-border rounded-md bg-input text-sm resize-none"
              />
            </Field>
            <StatusMsg msg={aiMsg} />
            <SaveButton loading={false} onClick={handleAiSave} label="保存 AI 设置" />
          </div>
        </div>
      )}
    </div>
  )
}

function TabButton({
  active,
  onClick,
  icon: Icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: typeof UserIcon
  label: string
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors ${
        active
          ? 'border-primary text-primary'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      <Icon className="w-4 h-4" />
      {label}
    </button>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[12px] font-medium text-muted-foreground mb-1.5">{label}</label>
      {children}
    </div>
  )
}

function ToggleRow({
  label,
  desc,
  checked,
  onChange,
}: {
  label: string
  desc: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div
      onClick={() => onChange(!checked)}
      className="flex items-start gap-3 px-3 py-2 border border-border rounded-md cursor-pointer hover:bg-muted/50"
    >
      <div
        className={`mt-0.5 w-9 h-5 rounded-full transition-colors flex-shrink-0 relative ${
          checked ? 'bg-primary' : 'bg-border'
        }`}
      >
        <span
          className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform shadow ${
            checked ? 'translate-x-4' : 'translate-x-0.5'
          }`}
        />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-foreground">{label}</div>
        <div className="text-[11px] text-muted-foreground">{desc}</div>
      </div>
    </div>
  )
}

function StatusMsg({ msg }: { msg: { type: 'success' | 'error'; text: string } | null }) {
  if (!msg) return null
  return (
    <div
      className={`text-[12px] px-3 py-1.5 rounded-md flex items-center gap-1.5 ${
        msg.type === 'success'
          ? 'bg-success-light text-success'
          : 'bg-danger-light text-danger'
      }`}
    >
      {msg.type === 'success' ? <Check className="w-3 h-3" /> : null}
      {msg.text}
    </div>
  )
}

function SaveButton({
  loading,
  onClick,
  label,
}: {
  loading: boolean
  onClick: () => void
  label: string
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      className="px-4 py-2 bg-primary text-white rounded-md text-[13px] disabled:opacity-50 flex items-center gap-2 hover:bg-primary-hover"
    >
      {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
      {label}
    </button>
  )
}