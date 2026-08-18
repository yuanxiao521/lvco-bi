import { Navigate, Outlet, useLocation } from "react-router-dom";
import { tokenStore } from "../../api/client";
import { useAuthStore } from "../../stores/authStore";

export default function ProtectedRoute() {
  const location = useLocation();
  const hasToken = Boolean(tokenStore.getAccess());

  if (!hasToken) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  if (!useAuthStore.getState().user) {
    const access = tokenStore.getAccess();
    if (access) {
      useAuthStore.setState({ accessToken: access });
    }
  }

  return <Outlet />;
}