// 副作用 import (matcher の登録)。main.tsx の CSS import と同じ理由で規則を外す
// oxlint-disable import/no-unassigned-import
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
