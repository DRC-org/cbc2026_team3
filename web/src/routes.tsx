import { Navigate } from "react-router";
import type { RouteObject } from "react-router";

import { RootLayout } from "@/layouts/RootLayout";
import { robotLabel } from "@/lib/robotLabel";
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
      {
        path: "main-hand",
        element: <RobotControl robotKey="main_hand" label={robotLabel("main_hand")} />,
      },
      {
        path: "sub-hand",
        element: <RobotControl robotKey="sub_hand" label={robotLabel("sub_hand")} />,
      },
      { path: "*", element: <Navigate to={DEFAULT_TAB_PATH} replace /> },
    ],
  },
];
