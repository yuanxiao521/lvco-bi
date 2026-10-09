import { create } from 'zustand';

interface UIState {
  sidebarCollapsed: boolean;
  aiAssistantOpen: boolean;
  toggleSidebar: () => void;
  toggleAIAssistant: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setAIAssistantOpen: (open: boolean) => void;
}

export const useUIStore = create<UIState>()((set) => ({
  sidebarCollapsed: false,
  aiAssistantOpen: false,

  toggleSidebar: () =>
    set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),

  toggleAIAssistant: () =>
    set((state) => ({ aiAssistantOpen: !state.aiAssistantOpen })),

  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),

  setAIAssistantOpen: (open) => set({ aiAssistantOpen: open }),
}));
