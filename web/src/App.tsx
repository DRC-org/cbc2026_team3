import { RouterProvider, createBrowserRouter } from "react-router";

import { applyLegacyHashRedirect } from "@/lib/tabs";
import { routes } from "@/routes";

applyLegacyHashRedirect();
const router = createBrowserRouter(routes);

export function App() {
  return <RouterProvider router={router} />;
}
