import { useEffect, useState, useCallback } from 'react'
import {
  Users,
  Loader2,
  Search,
  ChevronLeft,
  ChevronRight,
  Shield,
  Check,
} from 'lucide-react'
import {
  listUsers,
  updateUserRole,
  ROLE_LABELS,
  ROLE_COLORS,
} from '../../api/permissions'
import type { UserListItem, UserRole } from '../../api/permissions'
import { useAuthStore } from '../../stores/authStore'
import { useToast } from '../../components/ui/Toast'

const PAGE_SIZE = 20
const ALL_ROLES: UserRole[] = ['admin', 'editor', 'viewer']

export default function PermissionsPage() {
  const toast = useToast()
  const currentUser = useAuthStore((s) => s.user)
  const isAdmin = currentUser?.role === 'admin'

  const [items, setItems] = useState<UserListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState<'' | UserRole>('')
  const [pendingRole, setPendingRole] = useState<{ userId: string; role: UserRole } | null>(null)

  const fetchList = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listUsers({
        page,
        pageSize: PAGE_SIZE,
        search: search || undefined,
        role: roleFilter || undefined,
      })
      setItems(res.items)
      setTotal(res.total)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail?.message || '加载用户列表失败')
      setItems([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [page, search, roleFilter, toast])

  useEffect(() => {
    fetchList()
  }, [fetchList])

  useEffect(() => {
    setPage(1)
  }, [search, roleFilter])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const handleRoleChange = async (user: UserListItem, newRole: UserRole) => {
    if (newRole === user.role) return
    if (!isAdmin) {
      toast.error('仅管理员可调整用户角色')
      return
    }
    const ok = await toast.confirm(
      `确定将「${user.displayName || user.email}」的角色从「${ROLE_LABELS[user.role]}」调整为「${ROLE_LABELS[newRole]}」？`,
    )
    if (!ok) return
    setPendingRole({ userId: user.id, role: newRole })
    try {
      await updateUserRole(user.id, newRole)
      setItems((prev) =>
        prev.map((it) => (it.id === user.id ? { ...it, role: newRole } : it)),
      )
      toast.success('角色已更新')
    } catch (e: any) {
      const msg = e?.response?.data?.detail?.message || '更新失败'
      toast.error(msg)
    } finally {
      setPendingRole(null)
    }
  }

  return (
    <div className="flex-1 p-6 space-y-5">
      {/* 顶部 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md bg-primary-light flex items-center justify-center">
            <Users className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h1 className="text-[17px] font-semibold text-foreground">权限管理</h1>
            <p className="text-[12px] text-muted-foreground mt-0.5">
              查看与调整平台用户角色 · 共 {total} 位用户
              {!isAdmin && ' · 当前账号为只读模式'}
            </p>
          </div>
        </div>
        <button
          onClick={fetchList}
          className="px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted/50 text-muted-foreground"
        >
          刷新
        </button>
      </div>

      {/* 筛选 */}
      <div className="bg-white rounded-md shadow-card border border-border p-4 flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索邮箱或显示名称"
            className="w-full pl-9 pr-3 py-2 border border-border rounded-md bg-input text-sm"
          />
        </div>
        <select
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value as '' | UserRole)}
          className="px-3 py-2 border border-border rounded-md bg-input text-sm"
        >
          <option value="">全部角色</option>
          {ALL_ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_LABELS[r]}
            </option>
          ))}
        </select>
      </div>

      {/* 表格 */}
      <div className="bg-white rounded-md shadow-card border border-border overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            加载中...
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <Shield className="w-10 h-10 mb-2 opacity-40" />
            <p className="text-[13px]">没有匹配的用户</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="text-left font-medium px-4 py-2.5">用户</th>
                  <th className="text-left font-medium px-4 py-2.5">邮箱</th>
                  <th className="text-left font-medium px-4 py-2.5">角色</th>
                  <th className="text-right font-medium px-4 py-2.5">数据源</th>
                  <th className="text-right font-medium px-4 py-2.5">画布</th>
                  <th className="text-right font-medium px-4 py-2.5">仪表盘</th>
                  <th className="text-left font-medium px-4 py-2.5">注册时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => {
                  const isSelf = currentUser?.id === u.id
                  const isPending =
                    pendingRole?.userId === u.id && pendingRole?.role !== u.role
                  return (
                    <tr
                      key={u.id}
                      className="border-t border-border-light hover:bg-muted/30 transition-colors"
                    >
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2.5">
                          <div className="w-8 h-8 rounded-full bg-primary-light text-primary flex items-center justify-center text-[12px] font-medium">
                            {u.displayName?.slice(0, 1).toUpperCase() || u.email.slice(0, 1).toUpperCase()}
                          </div>
                          <div>
                            <div className="font-medium text-foreground flex items-center gap-1.5">
                              {u.displayName || '未设置'}
                              {isSelf && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-info-light text-info">
                                  你
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground font-mono text-[12px]">
                        {u.email}
                      </td>
                      <td className="px-4 py-2.5">
                        {isAdmin ? (
                          <select
                            value={u.role}
                            disabled={isPending}
                            onChange={(e) => handleRoleChange(u, e.target.value as UserRole)}
                            className={`px-2 py-1 border border-border rounded-md text-[12px] font-medium ${ROLE_COLORS[u.role]}`}
                          >
                            {ALL_ROLES.map((r) => (
                              <option key={r} value={r}>
                                {ROLE_LABELS[r]}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <span
                            className={`inline-block px-2 py-0.5 rounded text-[11px] font-medium ${ROLE_COLORS[u.role]}`}
                          >
                            {ROLE_LABELS[u.role]}
                          </span>
                        )}
                        {isPending && (
                          <Loader2 className="w-3 h-3 inline-block ml-2 animate-spin text-muted-foreground" />
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-right tabular-nums">{u.datasourceCount}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums">{u.canvasCount}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums">{u.dashboardCount}</td>
                      <td className="px-4 py-2.5 text-muted-foreground text-[12px]">
                        {u.createdAt ? new Date(u.createdAt).toLocaleString('zh-CN') : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* 分页 */}
        {total > 0 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-border-light text-[12px] text-muted-foreground">
            <div className="flex items-center gap-2">
              <Check className="w-3.5 h-3.5" />
              共 {total} 条 · 第 {page} / {totalPages} 页
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="p-1.5 border border-border rounded-md hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="p-1.5 border border-border rounded-md hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
