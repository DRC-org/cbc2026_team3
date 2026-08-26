import { describe, expect, it } from "vitest";

import {
  COURT_LABEL,
  PHASE_BAND_CLASS,
  PHASE_LABEL,
  PHASE_TONE,
  isDuringMatch,
  isSetupPhase,
} from "@/lib/phase";
import type { MatchPhase } from "@/lib/protocol";

const ALL_PHASES: MatchPhase[] = ["setup", "ready", "match", "finished"];

describe("isSetupPhase", () => {
  it("setup と ready を準備フェーズとして扱う", () => {
    expect(isSetupPhase("setup")).toBe(true);
    expect(isSetupPhase("ready")).toBe(true);
    expect(isSetupPhase("match")).toBe(false);
    expect(isSetupPhase("finished")).toBe(false);
  });
});

/**
 * サーバーのフェーズゲート (`lib/match_state.py`) の写し。
 * PHASES_DURING_MATCH = {match}、PHASES_OUTSIDE_MATCH はその補集合なので、
 * この 1 つの判定で両方に答えられる。
 */
describe("isDuringMatch", () => {
  it("match だけを試合中とする", () => {
    expect(isDuringMatch("match")).toBe(true);
    expect(isDuringMatch("setup")).toBe(false);
    expect(isDuringMatch("ready")).toBe(false);
  });

  it("finished は試合中ではない (PID 変更も動作確認もサーバーは通す)", () => {
    // 画面レイアウトの都合で match と finished をまとめて扱いたくなるが、
    // コマンドの可否は別物。ここを混ぜると試合終了後に操作を塞いでしまう
    expect(isDuringMatch("finished")).toBe(false);
  });

  it("準備フェーズと試合中が同時に成立することはない", () => {
    for (const phase of ALL_PHASES) {
      expect(isSetupPhase(phase) && isDuringMatch(phase)).toBe(false);
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
