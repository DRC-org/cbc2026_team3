import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRobotSocket } from "@/hooks/useRobotSocket";
import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";
import contract from "@/test/ws-contract.json";

/**
 * サーバーの実配信 (`ws-contract.json`) が UI の受信経路を通ることを固定する。
 *
 * 型アサーションだけでは足りない。型が合っていても `useRobotSocket` の受信条件が
 * 弾けばメッセージは捨てられ、画面には何も出ない。実際に `health_change` は
 * `robot` を含まないまま配信されており、UI は `typeof msg.robot === "string"` を
 * 受信条件にしていたので実機で 100% 捨てられていた。両側のテストが揃って
 * 見逃したのは、TS 側がサンプルを自分で捏造していたからである。
 *
 * したがってここでは **契約ファイルの実サンプルを reducer に流し込み**、
 * 状態が期待どおり更新されることだけを検証する。サンプルを手で書き写してはならない
 * (写した瞬間に「想像した契約」へ逆戻りする)。
 */

const URL = "ws://contract/ws";

type Sample = Record<string, unknown>;

const SAMPLES = contract.samples as unknown as Record<string, Sample>;
const EPOCH_SECONDS: number = contract.$placeholders.epoch_seconds;

type SocketResult = ReturnType<typeof useRobotSocket>;
type Expectation = (result: SocketResult, sample: Sample) => void;

/** state サンプルから UI が実際に読むフィールド。欠けたら画面のどこかが黙って壊れる */
const STATE_FIELDS_UI_READS = [
  "type",
  "robot",
  "sequence",
  "current_step",
  "step_index",
  "total_steps",
  "waiting_trigger",
  "running",
  "steps",
  "motors",
  "e_stop_active",
  "health",
  "safety",
] as const;

/**
 * サンプル名 → 受信後の期待。
 *
 * 契約ファイルにメッセージ型が増えると「未対応のサンプルがある」テストが落ちる。
 * サーバーが送り始めたものを UI が黙って捨て続ける状態を、ここで検出する。
 */
const EXPECTATIONS: Record<string, Expectation> = {
  state: (result, sample) => {
    const robot = sample.robot as string;
    // 受信した state はそのまま保持される (モータ名等をハードコードしないため)
    expect(result.states[robot]).toEqual(sample);
    expect(result.eStopActive).toBe(sample.e_stop_active);
    // 実行状態は推測せずサーバーの running をそのまま持つ
    expect(result.states[robot].running).toBe(sample.running);
    // 安全機構 (ラッチ中の軸・保護ループの生死) も配信そのまま
    expect(result.states[robot].safety).toEqual(sample.safety);
  },

  match_state: (result, sample) => {
    expect(result.matchState).toEqual({
      court: sample.court,
      phase: sample.phase,
      can_start_match: sample.can_start_match,
      checklists: sample.checklists,
    });
  },

  health_change: (result, sample) => {
    expect(result.healthEvents).toHaveLength(1);
    expect(result.healthEvents[0]).toMatchObject({
      robot: sample.robot,
      level: sample.level,
      target: sample.target,
      from: sample.from,
      to: sample.to,
      message: sample.message,
    });
  },

  health_change_bus: (result, sample) => {
    expect(result.healthEvents).toHaveLength(1);
    expect(result.healthEvents[0]).toMatchObject({
      robot: sample.robot,
      target: sample.target,
      level: sample.level,
    });
  },

  e_stop_state: (result, sample) => {
    expect(result.eStopActive).toBe(sample.active);
    expect(result.eStopReason).toBeNull();
  },

  e_stop_state_with_reason: (result, sample) => {
    expect(result.eStopActive).toBe(true);
    // 「誰かが押したのか、機体が壊れたのか」を操縦者が区別できないと復旧手順を選べない
    expect(result.eStopReason).toBe(sample.reason);
  },

  command_rejected: (result, sample) => {
    expect(result.rejection).toMatchObject({
      command: sample.command,
      reason: sample.reason,
    });
  },

  motor_check_progress: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("running");
    expect(state.current).toBe(sample.current);
    expect(state.progress).toEqual({ index: sample.index, total: sample.total });
  },

  motor_check_record: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.records).toEqual([sample.record]);
  },

  motor_check_done: (result, sample) => {
    const snapshot = sample.snapshot as Record<string, unknown>;
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("completed");
    expect(state.snapshot).toEqual(snapshot);
    expect(state.records).toEqual(snapshot.records);
    // サーバーはエポック秒、Date はミリ秒。ここが取り違えられると実施時刻が 1970 年になる
    expect(state.finishedAtMs).toBe(EPOCH_SECONDS * 1000);
    expect(state.startedAtMs).toBe(EPOCH_SECONDS * 1000);
  },

  motor_check_error: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("error");
    expect(state.error).toBe(sample.message);
  },
};

function renderConnected() {
  const view = renderHook(() => useRobotSocket(URL));
  act(() => latestSocket().open());
  return view;
}

beforeEach(() => {
  installMockWebSocket();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WS 契約 (ws-contract.json)", () => {
  it("契約の全サンプルに TS 側の検証がある", () => {
    // サーバーが新しいメッセージ型を送り始めたのに UI が対応していない状態を、
    // 契約ファイルの再生成 (UPDATE_WS_CONTRACT=1) 時点で落とす
    expect(Object.keys(SAMPLES).toSorted()).toEqual(Object.keys(EXPECTATIONS).toSorted());
  });

  it("state サンプルに UI が読むフィールドが揃っている", () => {
    // 型は実行時に消えるので、フィールドの存在はここでしか守れない。
    // 例えば running が配信から落ちれば、UI は再び step_index からの推測へ逆戻りする
    for (const field of STATE_FIELDS_UI_READS) {
      expect(SAMPLES.state).toHaveProperty(field);
    }
  });

  describe.each(Object.keys(SAMPLES))("%s", (name) => {
    it("受信経路を通って状態へ反映される", () => {
      const expectation = EXPECTATIONS[name];
      if (!expectation) throw new Error(`契約サンプル ${name} に対応する検証がありません`);

      const { result } = renderConnected();
      act(() => latestSocket().receive(SAMPLES[name]));

      expectation(result.current, SAMPLES[name]);
    });
  });
});
