/**
 * 画布跨图表联动筛选 Store（模块 B2）。
 *
 * 语义：点击任意图表的维度值（如柱状图的"北京"柱子）→ 全局生效一个筛选
 * → 其他图表按该维度值重新查询；再次点击同一值或点 chip 的 × 取消筛选。
 * 同一时刻只保留一个联动筛选（PowerBI 交叉筛选的轻量版）。
 */
import { create } from "zustand";

export interface CrossFilter {
  /** 维度字段名（数据源列名） */
  field: string;
  /** 维度值（如"北京"） */
  value: string;
}

interface CrossFilterState {
  filter: CrossFilter | null;
  /** 直接设置/清空联动筛选 */
  setFilter: (filter: CrossFilter | null) => void;
  /** 切换：同 field+value 再点一次取消，否则覆盖 */
  toggleFilter: (filter: CrossFilter) => void;
  /** 清空联动筛选 */
  clear: () => void;
}

export const useCrossFilterStore = create<CrossFilterState>((set, get) => ({
  filter: null,
  setFilter: (filter) => set({ filter }),
  toggleFilter: (filter) => {
    const cur = get().filter;
    set({ filter: cur && cur.field === filter.field && cur.value === filter.value ? null : filter });
  },
  clear: () => set({ filter: null }),
}));
