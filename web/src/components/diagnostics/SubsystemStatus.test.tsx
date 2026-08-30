import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import type { HealthSnapshot, MotorState, SafetyState } from "@/lib/protocol";
import { renderWithRobot } from "@/test/robotContext";

const HEALTH: HealthSnapshot = {
  timestamp: 0,
  overall: "ok",
  buses: [
    {
      name: "can_m3508",
      channel: "can0",
      state: "ok",
      last_tx_at: null,
      last_rx_at: null,
      tx_error_count: 0,
      rx_error_count: 0,
      bus_off: false,
    },
  ],
  motors: [],
  detail: null,
};

const MOTORS: Record<string, MotorState> = {
  y_axis_r: { pos: 0, vel: 0, torque: 0, temp: 30, pid: null },
};

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    loops_running: true,
    monitors_running: true,
    position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
    sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
    refreshers_running: true,
    target_refreshers: [{ motors: ["gripper"], running: true, paused: false }],
    ...over,
  };
}

describe("SubsystemStatus", () => {
  it("平常時は 1 行に畳み、安全機構の行を足さない", () => {
    // 試合中の操縦者が画面へ視線を戻すのは一瞬しかない。平常時は静かに保つ
    renderWithRobot(<SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/同期ずれ/)).not.toBeInTheDocument();
  });

  it("同期ずれラッチは畳んだ状態を上書きして開き、復旧手順まで出す", () => {
    // 緊急停止を解除してもその軸は動かない。解除操作だけを繰り返させてはならない
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
    expect(screen.getByText("y_axis")).toBeInTheDocument();
    expect(screen.getByText(/解除し直して/)).toBeInTheDocument();
  });

  it("保護ループの停止を自分から主張する", () => {
    // WS は繋がったままモータ状態も届き続けるので、ここに出さないと誰も気付けない
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({
          loops_running: false,
          position_loops: [
            { bus: "can_m3508", running: false, paused: false, sync_violations: [] },
          ],
        })}
      />,
    );

    expect(screen.getByText("位置制御ループ停止")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("目標値再送の停止を自分から主張する", () => {
    // 20Hz の再送が止まると 500ms 後にファーム側ウォッチドッグが効き、
    // グリッパ・コンベア・壁が無反応になる。WS は繋がったままなので画面からは原因が分からない
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({
          refreshers_running: false,
          target_refreshers: [{ motors: ["gripper", "conveyor"], running: false, paused: false }],
        })}
      />,
    );

    expect(screen.getByText("目標値再送停止")).toBeInTheDocument();
    expect(screen.getByText("gripper, conveyor")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("異常中は操縦者が畳もうとしても畳めない", async () => {
    // 「自分から開く」だけでは足りない。試合中に一度畳めてしまえば、
    // その後に出た異常も畳んだままになり、見逃しの経路がそのまま残る
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    await userEvent.click(screen.getByRole("button", { expanded: true }));

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
  });

  it("異常中に畳もうとした操作は、解消した時点で効く", async () => {
    // 強制開示のまま操作を握り潰すと、異常が消えた後も 32 個の数字が
    // 試合の残り時間ずっと開いたままになる (平常時に静かでなくなる)
    // rerender で異常の解消を再現するため、ここは素の render を使う
    const { rerender } = render(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    await userEvent.click(screen.getByRole("button", { expanded: true }));
    rerender(<SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("異常中に触っていなければ、解消後も畳んだまま", async () => {
    const { rerender } = render(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    rerender(<SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("モータ過熱の警告でも自分から開く (安全機構の異常に限らない)", () => {
    // 開く条件を error だけに絞ると、焼損に向かう温度上昇を畳んだまま見逃す。
    // 過熱の判定はサーバー (config の temp_warning_c) が持ち、warning として届く
    renderWithRobot(
      <SubsystemStatus
        health={{
          ...HEALTH,
          motors: [
            {
              name: "y_axis_r",
              bus: "can_m3508",
              state: "warning",
              last_feedback_at: null,
              feedback_age_ms: 0,
              temperature: 90,
              detail: null,
            },
          ],
        }}
        motors={{ y_axis_r: { pos: 0, vel: 0, torque: 0, temp: 90, pid: null } }}
        safety={safety()}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("要確認 1 件")).toBeInTheDocument();
  });

  /**
   * 温度の色分けの境界はサーバーの config だけが持つ。呼び出し元 → SubsystemStatus →
   * MotorSummary → MotorStatus の受け渡しが 1 段でも切れると、config を変えても
   * 画面の色だけが変わらない (数値は出続けるので画面からは気付けない)。
   */
  it("温度しきい値を渡すと過熱モータに色が付く", () => {
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={{ y_axis_r: { pos: 0, vel: 0, torque: 0, temp: 90, pid: null } }}
        safety={safety()}
        tempThresholds={{ warning: 65, critical: 80 }}
        defaultOpen
      />,
    );

    expect(screen.getByText("90.0")).toHaveClass("text-error");
  });

  it("しきい値が未取得なら色を付けない (UI が独自の境界を持たない)", () => {
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={{ y_axis_r: { pos: 0, vel: 0, torque: 0, temp: 90, pid: null } }}
        safety={safety()}
        defaultOpen
      />,
    );

    const temp = screen.getByText("90.0");
    expect(temp).not.toHaveClass("text-error");
    expect(temp).not.toHaveClass("text-warning");
  });

  it("平常時は操縦者の操作で開閉できる", async () => {
    // 強制開示は異常時だけ。平常時まで開きっぱなしにすると数字の海に戻る
    renderWithRobot(<SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} />);

    await userEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { expanded: true }));
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  /**
   * サーバーはヘルス計算が失敗したとき overall=down・内訳空・detail 付きを配信する。
   * 内訳が空だからと緑の「異常なし」を出して畳んだままにすると、サーバーが
   * 「もう健全性を判断できない」と言っている状態が画面上で正常として消える。
   */
  it("サーバーが判定不能を配信したら、理由まで出して自分から開く", () => {
    renderWithRobot(
      <SubsystemStatus
        health={{
          timestamp: 0,
          overall: "down",
          buses: [],
          motors: [],
          detail: "ヘルス計算に失敗しました: boom",
        }}
        motors={MOTORS}
        safety={safety()}
      />,
    );

    expect(screen.queryByText("異常なし")).not.toBeInTheDocument();
    expect(screen.getByText("健全性 判定不能")).toBeInTheDocument();
    expect(screen.getByText(/ヘルス計算に失敗しました: boom/)).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("判定を別の要素が担う画面では、判定チップも開閉も持たない", () => {
    // Monitor の準備画面は StartGate が「異常があるか」を最大の要素で答える。
    // 同じ文字列をこの見出しにも出すと、同じ事実が同じ画面に 2 回並ぶ
    renderWithRobot(
      <SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} showVerdict={false} />,
    );

    expect(screen.queryByText("異常なし")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    // 中身 (どのバス・どのモータか) は常に見えている
    expect(screen.getByText("can_m3508")).toBeInTheDocument();
  });
});
