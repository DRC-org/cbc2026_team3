import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRobotSocket } from "@/hooks/useRobotSocket";
import type { CheckRunSnapshot, MotorCheckRecord } from "@/lib/protocol";
import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";

const URL = "ws://test/ws";

function record(motor: string, over: Partial<MotorCheckRecord> = {}): MotorCheckRecord {
  return {
    motor,
    bus: "can0",
    started_at: 1,
    finished_at: 2,
    result: "passed",
    expected: 100,
    observed: 100,
    detail: null,
    ...over,
  };
}

/** 接続確立済みの hook を返す */
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
  vi.useRealTimers();
});

describe("接続", () => {
  it("指定 URL へ接続し、open で connected になる", () => {
    const { result } = renderHook(() => useRobotSocket(URL));
    expect(latestSocket().url).toBe(URL);
    expect(result.current.connected).toBe(false);

    act(() => latestSocket().open());
    expect(result.current.connected).toBe(true);
  });

  it("URL 未指定ならページ origin の /ws へ繋ぐ (別端末からの利用を成立させるため)", () => {
    renderHook(() => useRobotSocket());
    expect(latestSocket().url).toBe(`ws://${window.location.host}/ws`);
  });

  it("切断されると connected を落とし、一定時間後に再接続する", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useRobotSocket(URL));
    act(() => latestSocket().open());

    act(() => latestSocket().close());
    expect(result.current.connected).toBe(false);

    act(() => vi.advanceTimersByTime(3000));
    expect(latestSocket().url).toBe(URL);

    act(() => latestSocket().open());
    expect(result.current.connected).toBe(true);
  });

  it("url が変わると新しい接続先へ張り直し、その間は切断表示にする", () => {
    const NEXT = "ws://drc:8080/ws";
    const { result, rerender } = renderHook(({ url }) => useRobotSocket(url), {
      initialProps: { url: URL },
    });
    act(() => latestSocket().open());
    expect(result.current.connected).toBe(true);

    rerender({ url: NEXT });
    expect(latestSocket().url).toBe(NEXT);
    // 新しい接続が open するまでは指令が届かない。繋がっている表示を残してはならない
    expect(result.current.connected).toBe(false);

    act(() => latestSocket().open());
    expect(result.current.connected).toBe(true);
  });

  it("接続先切替後、旧接続の close で旧 URL へ再接続しない", () => {
    vi.useFakeTimers();
    const NEXT = "ws://drc:8080/ws";
    const { rerender } = renderHook(({ url }) => useRobotSocket(url), {
      initialProps: { url: URL },
    });
    const old = latestSocket();
    act(() => old.open());

    rerender({ url: NEXT });
    // 実 WebSocket の close イベントは切替より後に非同期で届く
    act(() => old.close());
    act(() => vi.advanceTimersByTime(3000));

    expect(latestSocket().url).toBe(NEXT);
  });

  it("接続先切替後、旧接続から届いた state で画面を上書きしない", () => {
    const { result, rerender } = renderHook(({ url }) => useRobotSocket(url), {
      initialProps: { url: URL },
    });
    const old = latestSocket();
    act(() => old.open());

    rerender({ url: "ws://drc:8080/ws" });
    act(() => old.receive({ type: "state", robot: "main_hand", step_index: 9 }));

    expect(result.current.states).toEqual({});
  });
});

describe("送信", () => {
  it("接続済みなら JSON 化して送る", () => {
    const { result } = renderConnected();
    act(() => result.current.send({ type: "trigger", robot: "main_hand" }));
    expect(latestSocket().sentJson()).toEqual([{ type: "trigger", robot: "main_hand" }]);
  });

  it("未接続では送信せず例外も投げない", () => {
    const { result } = renderHook(() => useRobotSocket(URL));
    act(() => result.current.send({ type: "trigger" }));
    expect(latestSocket().sent).toHaveLength(0);
  });

  it("送れたかどうかを呼び出し側へ返す", () => {
    // 呼び出し側が「届いた前提」で楽観的に状態を変えると、切断中に緊急停止を
    // 押しただけで全画面が「停止しました」と表示する (機体は動き続けている)
    const { result } = renderHook(() => useRobotSocket(URL));
    let sent: boolean | undefined;
    act(() => {
      sent = result.current.send({ type: "trigger" });
    });
    expect(sent).toBe(false);

    act(() => latestSocket().open());
    act(() => {
      sent = result.current.send({ type: "trigger" });
    });
    expect(sent).toBe(true);
  });
});

describe("state メッセージ", () => {
  it("ロボット単位で状態を保持し、他機の状態を消さない", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() => socket.receive({ type: "state", robot: "main_hand", step_index: 1 }));
    act(() => socket.receive({ type: "state", robot: "sub_hand", step_index: 5 }));

    expect(result.current.states.main_hand.step_index).toBe(1);
    expect(result.current.states.sub_hand.step_index).toBe(5);
  });

  it("e_stop_active を含む場合は非常停止状態へ反映する", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() => socket.receive({ type: "state", robot: "main_hand", e_stop_active: true }));
    expect(result.current.eStopActive).toBe(true);

    act(() => socket.receive({ type: "state", robot: "main_hand", e_stop_active: false }));
    expect(result.current.eStopActive).toBe(false);
  });

  it("robot が無い state は無視する", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "state", step_index: 3 }));
    expect(result.current.states).toEqual({});
  });
});

describe("e_stop_state メッセージ", () => {
  it("active を反映する", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));
    expect(result.current.eStopActive).toBe(true);
  });

  it("active が真偽値でなければ無視する", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "e_stop_state", active: "yes" }));
    expect(result.current.eStopActive).toBe(false);
  });
});

describe("match_state メッセージ", () => {
  it("サーバー値で試合状態を全面的に置き換える", () => {
    const { result } = renderConnected();

    act(() =>
      latestSocket().receive({
        type: "match_state",
        court: "blue",
        phase: "match",
        can_start_match: true,
        checklists: { main_hand: { items: [], completed: true } },
        timer: { running: true, elapsed_ms: 3_000, duration_ms: 180_000 },
      }),
    );

    expect(result.current.matchState).toEqual({
      court: "blue",
      phase: "match",
      can_start_match: true,
      checklists: { main_hand: { items: [], completed: true } },
      timer: { running: true, elapsed_ms: 3_000, duration_ms: 180_000 },
    });
  });

  it("checklists が欠けても既定値で成立させる", () => {
    const { result } = renderConnected();

    act(() =>
      latestSocket().receive({
        type: "match_state",
        court: "red",
        phase: "ready",
      }),
    );

    expect(result.current.matchState.checklists).toEqual({});
    expect(result.current.matchState.can_start_match).toBe(false);
  });
});

describe("command_rejected メッセージ", () => {
  it("拒否内容を保持し、clearRejection で消える", () => {
    const { result } = renderConnected();

    act(() =>
      latestSocket().receive({
        type: "command_rejected",
        command: "match_start",
        reason: "チェックリスト未完了",
      }),
    );

    expect(result.current.rejection).toMatchObject({
      command: "match_start",
      reason: "チェックリスト未完了",
    });

    act(() => result.current.clearRejection());
    expect(result.current.rejection).toBeNull();
  });

  it("command / reason が欠けても空文字で受ける", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "command_rejected" }));
    expect(result.current.rejection).toMatchObject({ command: "", reason: "" });
  });
});

describe("health_change メッセージ", () => {
  it("新しい順に積む", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() => socket.receive({ type: "health_change", robot: "main_hand", target: "can0" }));
    act(() => socket.receive({ type: "health_change", robot: "sub_hand", target: "can1" }));

    expect(result.current.healthEvents.map((e) => e.target)).toEqual(["can1", "can0"]);
  });

  it("直近 5 件を超えた分は捨てる (古い通知で画面を埋めないため)", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    for (let i = 0; i < 8; i++) {
      act(() => socket.receive({ type: "health_change", robot: "main_hand", target: `m${i}` }));
    }

    expect(result.current.healthEvents).toHaveLength(5);
    expect(result.current.healthEvents.map((e) => e.target)).toEqual([
      "m7",
      "m6",
      "m5",
      "m4",
      "m3",
    ]);
  });

  it("level 省略時は info 扱いにする", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "health_change", robot: "main_hand" }));
    expect(result.current.healthEvents[0].level).toBe("info");
  });
});

describe("motor_check_* メッセージ", () => {
  it("progress で running になり進捗を持つ", () => {
    const { result } = renderConnected();

    act(() =>
      latestSocket().receive({
        type: "motor_check_progress",
        robot: "main_hand",
        current: "lift",
        index: 1,
        total: 4,
      }),
    );

    const state = result.current.motorChecks.main_hand;
    expect(state.status).toBe("running");
    expect(state.current).toBe("lift");
    expect(state.progress).toEqual({ index: 1, total: 4 });
    expect(state.startedAtMs).not.toBeNull();
  });

  it("progress を重ねても開始時刻は最初の値を保つ", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() =>
      socket.receive({ type: "motor_check_progress", robot: "main_hand", index: 0, total: 2 }),
    );
    const startedAtMs = result.current.motorChecks.main_hand.startedAtMs;

    act(() =>
      socket.receive({ type: "motor_check_progress", robot: "main_hand", index: 1, total: 2 }),
    );
    expect(result.current.motorChecks.main_hand.startedAtMs).toBe(startedAtMs);
  });

  it("record は初出を末尾に追加し、同じモータは順序を保ったまま上書きする", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() =>
      socket.receive({ type: "motor_check_record", robot: "main_hand", record: record("a") }),
    );
    act(() =>
      socket.receive({ type: "motor_check_record", robot: "main_hand", record: record("b") }),
    );
    act(() =>
      socket.receive({
        type: "motor_check_record",
        robot: "main_hand",
        record: record("a", { result: "failed" }),
      }),
    );

    const records = result.current.motorChecks.main_hand.records;
    expect(records.map((r) => r.motor)).toEqual(["a", "b"]);
    expect(records[0].result).toBe("failed");
  });

  it("done では snapshot の records を正として上書きする", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() =>
      socket.receive({ type: "motor_check_record", robot: "main_hand", record: record("a") }),
    );

    const snapshot: CheckRunSnapshot = {
      robot: "main_hand",
      started_at: 10,
      finished_at: 20,
      overall: "partial",
      records: [record("a", { result: "failed" }), record("z")],
    };
    act(() => socket.receive({ type: "motor_check_done", robot: "main_hand", snapshot }));

    const state = result.current.motorChecks.main_hand;
    expect(state.status).toBe("completed");
    expect(state.current).toBeNull();
    // ワイヤはエポック秒。UI 状態は ms へ正規化されている必要がある
    expect(state.finishedAtMs).toBe(20_000);
    expect(state.records.map((r) => r.motor)).toEqual(["a", "z"]);
  });

  it("error で status を error にしてメッセージを保持する", () => {
    const { result } = renderConnected();

    act(() =>
      latestSocket().receive({
        type: "motor_check_error",
        robot: "main_hand",
        message: "CAN タイムアウト",
      }),
    );

    const state = result.current.motorChecks.main_hand;
    expect(state.status).toBe("error");
    expect(state.error).toBe("CAN タイムアウト");
    expect(state.current).toBeNull();
  });

  it("robot ごとに独立した状態を持つ", () => {
    const { result } = renderConnected();
    const socket = latestSocket();

    act(() =>
      socket.receive({ type: "motor_check_progress", robot: "main_hand", index: 0, total: 1 }),
    );
    act(() => socket.receive({ type: "motor_check_error", robot: "sub_hand", message: "ng" }));

    expect(result.current.motorChecks.main_hand.status).toBe("running");
    expect(result.current.motorChecks.sub_hand.status).toBe("error");
  });
});

describe("不正な入力", () => {
  it("JSON として壊れたメッセージを無視する", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive("{ not json"));
    expect(result.current.states).toEqual({});
  });

  it("未知の type を無視する", () => {
    const { result } = renderConnected();
    act(() => latestSocket().receive({ type: "unknown_event", robot: "main_hand" }));
    expect(result.current.states).toEqual({});
  });
});
