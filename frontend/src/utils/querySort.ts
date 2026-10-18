/**
 * 查询结果行排序（趋势图日期轴）。
 *
 * 背景：数据库 GROUP BY 不保证输出顺序 → 折线/柱状的日期轴乱跳（实测）。
 * 渲染层已做一次排序兜底，但**落库**也要有序：autosave 会把前端 chartResults 写回
 * 后端 blocks._chartResult，若这里不排序，DB 里存的仍是乱序（后续 Agent 读画布会乱）。
 */
const DATE_RE = /^\d{4}-\d{2}(-\d{2})?/;
const DATE_DIM_MARKS = ['date', 'time', 'day', 'month', 'year', '日期', '时间', '月份', '年份'];

function looksLikeDate(v: unknown): boolean {
  return DATE_RE.test(String(v ?? ''));
}

/**
 * 日期维度行排序：仅当首维度名像日期、且**全部**首列值都像日期时按值升序。
 * 其余情况原样返回（保护「按销售额排名」这类有业务语义的顺序）。
 */
export function sortRowsByDate<T extends Record<string, unknown>>(
  dimensions: string[] | undefined,
  rows: T[] | undefined,
): T[] | undefined {
  if (!rows || rows.length < 2 || !dimensions || dimensions.length === 0) return rows;
  const dim0 = String(dimensions[0] || '').toLowerCase();
  if (!DATE_DIM_MARKS.some((m) => dim0.includes(m))) return rows;
  // 可能被 date_trunc 桶化成 {dim}_{bucket} 别名
  const keys = Object.keys(rows[0] ?? {});
  const key = keys.find((k) => k === dimensions[0]) ?? keys.find((k) => k.startsWith(dimensions[0]));
  if (!key) return rows;
  if (!rows.every((r) => looksLikeDate(r[key]))) return rows;
  return [...rows].sort((a, b) => String(a[key] ?? '').localeCompare(String(b[key] ?? '')));
}
