import { describe, expect, it } from "vitest";

import { legacyHashTarget } from "@/lib/tabs";

describe("legacyHashTarget", () => {
  it("旧ハッシュ形式のブックマークを対応するパスへ読み替える", () => {
    expect(legacyHashTarget({ pathname: "/", search: "", hash: "#main-hand" })).toBe("/main-hand");
    expect(legacyHashTarget({ pathname: "/", search: "", hash: "#monitor" })).toBe("/monitor");
  });

  it("読み替え時もクエリを落とさない", () => {
    expect(legacyHashTarget({ pathname: "/", search: "?ws=drc:8080", hash: "#sub-hand" })).toBe(
      "/sub-hand?ws=drc:8080",
    );
  });

  it("未知のハッシュとパス指定済みの URL には介入しない", () => {
    expect(legacyHashTarget({ pathname: "/", search: "", hash: "#unknown" })).toBeNull();
    expect(legacyHashTarget({ pathname: "/", search: "", hash: "" })).toBeNull();
    expect(legacyHashTarget({ pathname: "/monitor", search: "", hash: "#main-hand" })).toBeNull();
  });
});
