// 副作用 import (matcher の登録)。main.tsx の CSS import と同じ理由で規則を外す
// oxlint-disable import/no-unassigned-import
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom は scrollIntoView を実装していない。現在地を画面内へ送る部品
// (`SequenceStepList` / `ChecklistItems` / `ManualAxisRow`) がこれを呼ぶので、
// ここで 1 度だけ埋める。**テストごとに stub してはならない** —— 次に自動スクロールを
// 足した部品のテストだけが「関数ではない」で落ち、原因が部品側にあるように見える
// (実際に 3 通りの対処が混在していた: 個別 stub / オプショナル呼び出し / 未対処)。
Element.prototype.scrollIntoView ??= () => {};
