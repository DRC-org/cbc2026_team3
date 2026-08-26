import { describe, expect, it } from "vitest";

import type { MatchPhase } from "@/hooks/useRobotSocket";
import {
  COURT_LABEL,
  PHASE_BAND_CLASS,
  PHASE_LABEL,
  PHASE_TONE,
  isMatchPhase,
  isSetupPhase,
} from "@/lib/phase";

const ALL_PHASES: MatchPhase[] = ["setup", "ready", "match", "finished"];

describe("isSetupPhase / isMatchPhase", () => {
  it("setup と ready を準備フェーズとして扱う", () => {
    expect(isSetupPhase("setup")).toBe(true);
    expect(isSetupPhase("ready")).toBe(true);
    expect(isSetupPhase("match")).toBe(false);
    expect(isSetupPhase("finished")).toBe(false);
  });

  it("match と finished を試合フェーズとして扱う", () => {
    expect(isMatchPhase("match")).toBe(true);
    expect(isMatchPhase("finished")).toBe(true);
    expect(isMatchPhase("setup")).toBe(false);
    expect(isMatchPhase("ready")).toBe(false);
  });

  it("どのフェーズも準備・試合のどちらか一方に必ず分類される", () => {
    for (const phase of ALL_PHASES) {
      expect(isSetupPhase(phase) !== isMatchPhase(phase)).toBe(true);
    }
  });
});

describe("ラベル・配色テーブル", () => {
  it("全フェーズにラベルと配色が定義されている", () => {
    for (const phase of ALL_PHASES) {
      expect(PHASE_LABEL[phase]).toBeTruthy();
      expect(PHASE_BAND_CLASS[phase]).toBeTruthy();
      expect(PHASE_TONE[phase]).toBeTruthy();
    }
  });

  it("フェーズ帯の色は一瞥で区別できるよう全て異なる", () => {
    const bands = ALL_PHASES.map((p) => PHASE_BAND_CLASS[p]);
    expect(new Set(bands).size).toBe(ALL_PHASES.length);
  });

  it("コートのラベルが定義されている", () => {
    expect(COURT_LABEL.red).toBe("赤コート");
    expect(COURT_LABEL.blue).toBe("青コート");
  });
});
