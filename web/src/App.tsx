import { RouterProvider, createBrowserRouter } from "react-router";

import { applyLegacyHashRedirect } from "@/lib/tabs";
import { routes } from "@/routes";

// createBrowserRouter は生成時点の location を読み取る。旧ブックマークの読み替えは
// この 2 行の順序に依存しているので、間に処理を挟んだり入れ替えたりしてはならない
applyLegacyHashRedirect();
const router = createBrowserRouter(routes);

export function App() {
  return <RouterProvider router={router} />;
}
