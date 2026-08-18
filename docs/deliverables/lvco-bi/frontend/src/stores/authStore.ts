import { create } from 'zustand';
import type { UserInfo } from '../api/types';

interface AuthState {
  user: UserInfo | null;
  accessToken: string | null;
  refreshToken: string | null;
  setUser: (user: UserInfo | null) => void;
  setTokens: (access: string, refresh: string) => void;
  login: (user: UserInfo, access: string, refresh: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()((set) => ({
  user: null,
  accessToken: null,
  refreshToken: null,

  setUser: (user) => set({ user }),

  setTokens: (access, refresh) =>
    set({ accessToken: access, refreshToken: refresh }),

  login: (user, access, refresh) =>
    set({ user, accessToken: access, refreshToken: refresh }),

  logout: () =>
    set({ user: null, accessToken: null, refreshToken: null }),
}));
