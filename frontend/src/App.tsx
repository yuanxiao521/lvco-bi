import { Routes, Route } from "react-router-dom";
import AppLayout from "./components/layout/AppLayout";
import ProtectedRoute from "./components/layout/ProtectedRoute";
import FreeCanvas from "./pages/FreeCanvas";
import DashboardList from "./pages/Dashboard";
import DashboardDetail from "./pages/Dashboard/Detail";
import ReportCenter from "./pages/ReportCenter";
import ReportView from "./pages/ReportCenter/View";
import DataSource from "./pages/DataSource";
import StatisticsPage from "./pages/Statistics";
import AIChat from "./pages/AIChat";
import AccountSettings from "./pages/AccountSettings";
import Templates from "./pages/Templates";
import Trash from "./pages/Trash";
import Notifications from "./pages/Notifications";
import Permissions from "./pages/Permissions";
import Audit from "./pages/Audit";
import Login from "./pages/Login";
import Register from "./pages/Register";
import ForgotPassword from "./pages/ForgotPassword";
import SharePage from "./pages/Share";
import { useNotificationStream } from "./hooks/useNotificationStream";
import ErrorBoundary from "./components/ErrorBoundary";

/** 登录后全局订阅 SSE 通知流。包一层 panel 级 ErrorBoundary，
 *  防止 useNotificationStream 内部异常（EventSource / 解析失败）导致整页白屏。 */
function NotificationStreamProvider({ children }: { children: React.ReactNode }) {
  useNotificationStream();
  return <>{children}</>;
}

export default function App() {
  return (
    <>
      <ErrorBoundary scope="page" fallbackTitle="应用初始化失败">
        <NotificationStreamProvider>
          <Routes>
            <Route path="/share/report/:token" element={<SharePage />} />
            <Route path="/share/:token" element={<SharePage />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/forgot-password" element={<ForgotPassword />} />
            <Route element={<ProtectedRoute />}>
              <Route element={<AppLayout />}>
                <Route path="/" element={<FreeCanvas />} />
                <Route path="/dashboard" element={<DashboardList />} />
                <Route path="/dashboard/:id" element={<DashboardDetail />} />
                <Route path="/report-center/:id" element={<ReportView />} />
                <Route path="/report-center" element={<ReportCenter />} />
                <Route path="/data-source" element={<DataSource />} />
                <Route path="/statistics" element={<StatisticsPage />} />
                <Route path="/ai-chat" element={<AIChat />} />
                <Route path="/account-settings" element={<AccountSettings />} />
                <Route path="/templates" element={<Templates />} />
                <Route path="/trash" element={<Trash />} />
                <Route path="/notifications" element={<Notifications />} />
                <Route path="/permissions" element={<Permissions />} />
                <Route path="/audit" element={<Audit />} />
              </Route>
            </Route>
          </Routes>
        </NotificationStreamProvider>
      </ErrorBoundary>
    </>
  );
}