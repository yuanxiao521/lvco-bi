import { create } from 'zustand';
import type { CanvasBlock } from '../api/types';

type CanvasBlockLike = CanvasBlock & { id?: string };

// 画布状态接口定义
interface CanvasState {
  // 画布上的所有块列表
  blocks: CanvasBlock[];
  // 当前选中的块 ID，没有选中时为 null
  selectedBlockId: string | null;
  // 向画布添加一个新块
  addBlock: (block: CanvasBlock) => void;
  // 根据 ID 从画布移除指定块
  removeBlock: (id: string) => void;
  // 将块从 fromIndex 移动到 toIndex，实现拖拽排序
  reorderBlocks: (fromIndex: number, toIndex: number) => void;
  // 设置当前选中的块 ID（传入 null 表示取消选中）
  setSelectedBlockId: (id: string | null) => void;
}

export const useCanvasStore = create<CanvasState>()((set) => ({
  // 初始状态：空块列表
  blocks: [],
  // 初始状态：无选中块
  selectedBlockId: null,

  // 添加块：将新 block 追加到 blocks 数组末尾
  addBlock: (block) =>
    set((state) => ({ blocks: [...state.blocks, block] })),

  // 移除块：根据 id 过滤掉目标块，若移除的是当前选中块则同时清空选中状态
  removeBlock: (id) =>
    set((state) => ({
      blocks: state.blocks.filter(
        (b) => (b as CanvasBlockLike).id !== id
      ),
      selectedBlockId: state.selectedBlockId === id ? null : state.selectedBlockId,
    })),

  // 重排块：从 fromIndex 取出块插入到 toIndex 位置，实现拖拽排序效果
  reorderBlocks: (fromIndex, toIndex) =>
    set((state) => {
      const newBlocks = [...state.blocks];
      const [moved] = newBlocks.splice(fromIndex, 1);
      if (!moved) return state;
      newBlocks.splice(toIndex, 0, moved);
      return { blocks: newBlocks };
    }),

  // 设置选中块 ID：更新 selectedBlockId 为指定值或 null
  setSelectedBlockId: (id) => set({ selectedBlockId: id }),
}));
