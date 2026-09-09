import { Navigate } from "react-router";
import type { RouteObject } from "react-router";

import { RootLayout } from "@/layouts/RootLayout";
import { DEFAULT_TAB_PATH } from "@/lib/tabs";
import { Dashboard } from "@/pages/Dashboard";
import { RobotControl } from "@/pages/RobotControl";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <RootLayout />,
    children: [
      { index: true, element: <Navigate to={DEFAULT_TAB_PATH} replace /> },
      { path: "monitor", element: <Dashboard /> },
      { path: "main-hand", element: <RobotControl robotKey="main_hand" label="メインハンド" /> },
      { path: "sub-hand", element: <RobotControl robotKey="sub_hand" label="サブハンド" /> },
      { path: "*", element: <Navigate to={DEFAULT_TAB_PATH} replace /> },
    ],
  },
];
