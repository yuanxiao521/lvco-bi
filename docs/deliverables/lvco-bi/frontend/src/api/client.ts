import axios from "axios";
import type { AxiosError, AxiosResponse } from "axios";
import { useAuthStore } from "../stores/authStore";
import type { UserInfo } from "../types/user";

const ACCESS_TOKEN_KEY = "access_token";
const REFRESH_TOKEN_KEY = "refresh_token";
const USER_INFO_KEY = "user_info";

export const tokenStore = {
  getAccess: (): string | null => localStorage.getItem(ACCESS_TOKEN_KEY),
  getRefresh: (): string | null => localStorage.getItem(REFRESH_TOKEN_KEY),
  getUser: (): UserInfo | null => {
    const raw = localStorage.getItem(USER_INFO_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as UserInfo;
    } catch {
      return null;
    }
  },
  set: (access: string, refresh: string): void => {
    localStorage.setItem(ACCESS_TOKEN_KEY, access);
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
  },
  setAccess: (access: string): void => {
    localStorage.setItem(ACCESS_TOKEN_KEY, access);
  },
  setUser: (user: UserInfo): void => {
    localStorage.setItem(USER_INFO_KEY, JSON.stringify(user));
  },
  clear: (): void => {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    localStorage.removeItem(USER_INFO_KEY);
  },
};

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1",
  timeout: 30000,
});

apiClient.interceptors.request.use((config) => {
  const token = tokenStore.getAccess();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const cachedUser = tokenStore.getUser();
  if (cachedUser && !useAuthStore.getState().user) {
    useAuthStore.setState({ user: cachedUser });
  }
  return config;
});

let isRefreshing = false;
let pendingQueue: Array<(token: string) => void> = [];

apiClient.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error: AxiosError) => {
    const original = error.config as
      | (typeof error.config & { _retry?: boolean })
      | undefined;
    const status = error.response?.status;

    if (status === 401 && original && !original._retry) {
      const refresh = tokenStore.getRefresh();
      if (refresh && !original.url?.endsWith("/auth/refresh")) {
        if (!isRefreshing) {
          isRefreshing = true;
          try {
            const resp = await axios.post(
              `${apiClient.defaults.baseURL}/auth/refresh`,
              { refreshToken: refresh }
            );
            const next = resp.data?.data?.accessToken as string | undefined;
            if (next) {
              tokenStore.setAccess(next);
              pendingQueue.forEach((cb) => cb(next));
              pendingQueue = [];
            }
            original._retry = true;
            if (original.headers) {
              original.headers.Authorization = `Bearer ${next}`;
            }
            return apiClient(original);
          } catch {
            tokenStore.clear();
            useAuthStore.getState().logout();
            pendingQueue = [];
            window.location.href = "/login";
          } finally {
            isRefreshing = false;
          }
        }
        return new Promise((resolve) => {
          pendingQueue.push((token) => {
            if (original.headers) {
              original.headers.Authorization = `Bearer ${token}`;
            }
            original._retry = true;
            resolve(apiClient(original));
          });
        });
      }

      tokenStore.clear();
      useAuthStore.getState().logout();
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }

    return Promise.reject(error);
  }
);

export function extractData<T>(response: AxiosResponse<T>): T {
  return response.data;
}

export function unwrapApi<T>(payload: { success: boolean; data?: T; error?: unknown }): T {
  if (!payload.success) {
    throw payload.error ?? new Error("API 请求失败");
  }
  return payload.data as T;
}

export default apiClient;