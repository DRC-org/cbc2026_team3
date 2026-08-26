import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import type { MotorCheckRecord, MotorCheckState } from "@/hooks/useRobotSocket";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

/** 2023-11-15 06:13:20 UTC。UI が使うのは常にエポック**ミリ秒** */
const FINISHED_AT_MS = 1_700_000_000_000;

function record(over: Partial<MotorCheckRecord> = {}): MotorCheckRecord {
  return {
    motor: "gripper",
    bus: "can_generic",
    started_at: 1_700_000_000,
    finished_at: 1_700_000_000,
    result: "passed",
    expected: 5,
    observed: 4.9,
    detail: null,
    ...over,
  };
}

function mount(over: Partial<MotorCheckState>) {
  renderWithRobot(<MotorCheckSummary robotName="main_hand" />, {
    motorChecks: { main_hand: { ...EMPTY_MOTOR_CHECK, ...over } },
  });
}

describe("MotorCheckSummary", () => {
  it("未実行では未実行と出す", () => {
    mount({});
    expect(screen.getByText("未実行")).toBeInTheDocument();
  });

  it("実施時刻を実際の時刻として出す (1970 年にしない)", () => {
    // 指差喚呼で「アクチュエータ動作確認 完了」にチェックする直前の唯一の判断材料。
    // サーバーはエポック秒、Date はミリ秒で、以前はここに秒を渡していた
    mount({
      status: "completed",
      records: [record()],
      snapshot: {
        robot: "main_hand",
        started_at: 1_700_000_000,
        finished_at: 1_700_000_000,
        overall: "ok",
        records: [record()],
      },
      startedAtMs: FINISHED_AT_MS,
      finishedAtMs: FINISHED_AT_MS,
    });

    const expected = new Date(FINISHED_AT_MS).toLocaleTimeString("ja-JP", { hour12: false });
    expect(screen.getByText(expected)).toBeInTheDocument();
    // 秒をそのまま Date へ渡すと 1970-01-20 になる
    expect(screen.queryByText(new Date(1_700_000_000).toLocaleTimeString("ja-JP"))).toBeNull();
  });

  it("合格数と総数を出す", () => {
    mount({
      status: "completed",
      records: [record(), record({ motor: "lift", result: "failed" })],
      snapshot: {
        robot: "main_hand",
        started_at: 1_700_000_000,
        finished_at: 1_700_000_000,
        overall: "partial",
        records: [],
      },
      finishedAtMs: FINISHED_AT_MS,
    });

    expect(screen.getByText("一部失敗")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });

  it("エラーは理由まで出す", () => {
    mount({ status: "error", error: "試合中は動作確認を実行できません" });
    expect(screen.getByText("エラー")).toBeInTheDocument();
    expect(screen.getByText("試合中は動作確認を実行できません")).toBeInTheDocument();
  });
});
