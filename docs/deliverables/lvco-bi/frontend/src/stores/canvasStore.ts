import { create } from 'zustand';
import type { CanvasBlock } from '../api/types';

type CanvasBlockLike = CanvasBlock & { id?: string };

interface CanvasState {
  blocks: CanvasBlock[];
  selectedBlockId: string | null;
  addBlock: (block: CanvasBlock) => void;
  removeBlock: (id: string) => void;
  reorderBlocks: (fromIndex: number, toIndex: number) => void;
  setSelectedBlockId: (id: string | null) => void;
}

export const useCanvasStore = create<CanvasState>()((set) => ({
  blocks: [],
  selectedBlockId: null,

  addBlock: (block) =>
    set((state) => ({ blocks: [...state.blocks, block] })),

  removeBlock: (id) =>
    set((state) => ({
      blocks: state.blocks.filter(
        (b) => (b as CanvasBlockLike).id !== id
      ),
      selectedBlockId: state.selectedBlockId === id ? null : state.selectedBlockId,
    })),

  reorderBlocks: (fromIndex, toIndex) =>
    set((state) => {
      const newBlocks = [...state.blocks];
      const [moved] = newBlocks.splice(fromIndex, 1);
      if (!moved) return state;
      newBlocks.splice(toIndex, 0, moved);
      return { blocks: newBlocks };
    }),

  setSelectedBlockId: (id) => set({ selectedBlockId: id }),
}));
