import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { RobotProvider } from "@/context/RobotContext";
import type { HealthSnapshot, MotorState, SafetyState } from "@/lib/protocol";
import { motorState } from "@/test/motorState";
import { createRobotContext, renderWithRobot } from "@/test/robotContext";

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
      rx_down: false,
      rx_down_episodes: 0,
      may_affect_workpiece: false,
    },
  ],
  motors: [],
  detail: null,
};

const MOTORS: Record<string, MotorState> = {
  y_axis_r: motorState(),
};

function textCount(needle: string): number {
  return (document.body.textContent ?? "").split(needle).length - 1;
}

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    unenergized_motors: [],
    firmware_unconfirmed_motors: [],
    limit_latched: {},
    limit_blind_sensors: [],
    failed_tasks: [],
    reenergizing: false,
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
    renderWithRobot(
      <SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/同期ずれ/)).not.toBeInTheDocument();
  });

  it("defaultOpen が後から真になったら開く (再マウントされないので追従が要る)", () => {
    const context = createRobotContext();
    const panel = (open: boolean) => (
      <RobotProvider value={context}>
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety()}
          defaultOpen={open}
        />
      </RobotProvider>
    );
    const view = render(panel(false));
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();

    view.rerender(panel(true));

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("開いたときにセンサをモータ一覧とは別に出す", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety()}
        sensors={{ origin_sensor: { active: true, stale: false } }}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("origin_sensor")).toBeInTheDocument();
    expect(screen.getByText("接触")).toBeInTheDocument();
    expect(screen.getByText(/モータ 1$/)).toBeInTheDocument();
  });

  it("同期ずれラッチは畳んだ状態を上書きして開き、復旧手順まで出す", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ y_axis")).toBeInTheDocument();
    expect(screen.getByText(/解除し直して/)).toBeInTheDocument();
  });

  describe("安全機構の異常をチップと二重に描かない", () => {
    it("チップを出している画面では、先頭 1 件の文は 1 度だけ", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["rotate_l", "rotate_r"] })}
        />,
      );

      expect(textCount("無励磁のまま")).toBe(1);
      expect(textCount("rotate_l, rotate_r")).toBe(1);
      expect(screen.getByText(/再励磁/)).toBeInTheDocument();
    });

    it("チップを出さない画面 (showVerdict=false) では詳細行が文を引き受ける", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["rotate_l", "rotate_r"] })}
          showVerdict={false}
        />,
      );

      expect(screen.getByText("無励磁のまま")).toBeInTheDocument();
      expect(screen.getByText("rotate_l, rotate_r")).toBeInTheDocument();
    });

    it("2 件目以降はチップが言っていないので残す", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ sync_violations: ["y_axis"], unenergized_motors: ["rotate_l"] })}
        />,
      );

      expect(textCount("同期ずれラッチ")).toBe(1);
      expect(textCount("無励磁のまま")).toBe(1);
    });
  });

  describe("再励磁ボタン", () => {
    it("無励磁のモータがあり、かつ onReenergize を渡した画面にだけ出る", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["sub_lift"] })}
          onReenergize={() => {}}
        />,
      );

      expect(screen.getByRole("button", { name: "再励磁" })).toBeInTheDocument();
    });

    it("onReenergize を渡さない画面 (Monitor) では出さない", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["sub_lift"] })}
        />,
      );

      expect(screen.queryByRole("button", { name: "再励磁" })).not.toBeInTheDocument();
    });

    it("無励磁のモータが無ければ、onReenergize を渡していても出さない", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety()}
          onReenergize={() => {}}
        />,
      );

      expect(screen.queryByRole("button", { name: "再励磁" })).not.toBeInTheDocument();
    });

    it("無励磁以外の異常しか無ければ、onReenergize を渡していても出さない", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ sync_violations: ["y_axis"] })}
          onReenergize={() => {}}
        />,
      );

      expect(screen.getByText("同期ずれラッチ y_axis")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "再励磁" })).not.toBeInTheDocument();
    });

    it("処理中はサーバーの配信どおり押せなくなる", () => {
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["sub_lift"], reenergizing: true })}
          onReenergize={() => {}}
        />,
      );

      const button = screen.getByRole("button", { name: "処理中…" });
      expect(button).toBeDisabled();
      expect(screen.queryByRole("button", { name: "再励磁" })).not.toBeInTheDocument();
    });

    it("押すとコールバックが呼ばれる", async () => {
      const onReenergize = vi.fn();
      const user = userEvent.setup();
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ unenergized_motors: ["sub_lift"] })}
          onReenergize={onReenergize}
        />,
      );

      await user.click(screen.getByRole("button", { name: "再励磁" }));

      expect(onReenergize).toHaveBeenCalledOnce();
    });
  });

  it("保護ループの停止を自分から主張する", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
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

    expect(screen.getByText("位置制御ループ停止 can_m3508")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("目標値再送の停止を自分から主張する", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({
          refreshers_running: false,
          target_refreshers: [{ motors: ["gripper", "conveyor"], running: false, paused: false }],
        })}
      />,
    );

    expect(screen.getByText("目標値再送停止 gripper, conveyor")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("異常中は操縦者が畳もうとしても畳めない", async () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    await userEvent.click(screen.getByRole("button", { expanded: true }));

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ y_axis")).toBeInTheDocument();
  });

  it("異常中に畳もうとした操作は、解消した時点で効く", async () => {
    const { rerender } = render(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    await userEvent.click(screen.getByRole("button", { expanded: true }));
    rerender(<SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("異常中に触っていなければ、解消後も畳んだまま", async () => {
    const { rerender } = render(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    rerender(<SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("モータ過熱の警告でも自分から開く (安全機構の異常に限らない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
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
        motors={{ y_axis_r: motorState({ temp: 90 }) }}
        safety={safety()}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("要確認 1 件")).toBeInTheDocument();
  });

  it("温度しきい値を渡すと過熱モータに色が付く", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={{ y_axis_r: motorState({ temp: 90 }) }}
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
        connected
        health={HEALTH}
        motors={{ y_axis_r: motorState({ temp: 90 }) }}
        safety={safety()}
        defaultOpen
      />,
    );

    const temp = screen.getByText("90.0");
    expect(temp).not.toHaveClass("text-error");
    expect(temp).not.toHaveClass("text-warning");
  });

  it("平常時は操縦者の操作で開閉できる", async () => {
    renderWithRobot(
      <SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />,
    );

    await userEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { expanded: true }));
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("サーバーが判定不能を配信したら、理由まで出して自分から開く", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
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

  it("ワーク落下の恐れがあるバスを自分から主張する (判定は success のまま)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={{
          ...HEALTH,
          buses: [
            {
              ...HEALTH.buses[0],
              name: "can_generic",
              state: "ok",
              may_affect_workpiece: true,
              rx_down_episodes: 2,
            },
          ],
        }}
        motors={MOTORS}
        safety={safety()}
      />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText(/CAN 途絶 2回/)).toBeInTheDocument();
    expect(screen.getAllByText("can_generic").length).toBeGreaterThan(0);
    expect(screen.getByText(/ワークが落ちた可能性/)).toBeInTheDocument();
  });

  it("ワーク落下に無関係なバスの途絶は主張しない (can_dm3520 / can_m3508 相当)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={{
          ...HEALTH,
          buses: [{ ...HEALTH.buses[0], may_affect_workpiece: false, rx_down_episodes: 5 }],
        }}
        motors={MOTORS}
        safety={safety()}
      />,
    );

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/CAN 途絶/)).not.toBeInTheDocument();
  });

  it("エピソード 0 件なら、ワーク落下しうるバスでも何も出さない", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={{
          ...HEALTH,
          buses: [{ ...HEALTH.buses[0], may_affect_workpiece: true, rx_down_episodes: 0 }],
        }}
        motors={MOTORS}
        safety={safety()}
      />,
    );

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/CAN 途絶/)).not.toBeInTheDocument();

    // **開いても 1px も占めないこと。** 空の `<ul>` が残ると、中身の無い色付きの帯が
    // 平常時にずっと出たままになる。文字列の不在だけを見ていると、早期リターンを
    // 消しても帯だけが残った状態で緑になる
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(
      screen.queryByRole("list", { name: "ワーク落下の恐れがあるバス" }),
    ).not.toBeInTheDocument();
  });

  it("開いたときだけ INFO 未確認のモータを出す (判定・開閉は動かさない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: ["gripper"] })}
      />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText("版番号 未確認")).not.toBeInTheDocument();
  });

  it("開けば INFO 未確認のモータが見える", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: ["gripper"] })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText("版番号 未確認")).toBeInTheDocument();
    expect(screen.getByText("gripper")).toBeInTheDocument();
  });

  it("複数の未確認モータを 1 行にまとめず 1 件ずつ並べる", async () => {
    const user = userEvent.setup();
    const unconfirmed = ["valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6"];
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: unconfirmed })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getAllByText("版番号 未確認")).toHaveLength(unconfirmed.length);
    for (const name of unconfirmed) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  it("手当てとしてファームの焼き直しを案内する (配線を疑わせない)", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: ["gripper"] })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText(/ファームを焼き直して/)).toBeInTheDocument();
    expect(screen.queryByText(/配線を確認/)).not.toBeInTheDocument();
  });

  it("INFO 未確認が 0 件なら開いても何も出さない", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: [] })}
        defaultOpen
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.queryByText("版番号 未確認")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "版番号 未確認のモータ" })).not.toBeInTheDocument();
  });

  /**
   * リミットスイッチ保護 (`LimitLatchedNotice`)。**判定チップ (見出し) は動かさないが、
   * 畳んだままにはしない** —— 今まさに軸が止まっていて、逆向きへ退避するまで解けない。
   * 出さなければ操縦者から見えるのは「その向きへ指令しても動かない」だけになり、
   * 保護は軸ローカルなので他の軸は平常どおり動く (＝他に手掛かりが 1 つも無い)。
   */
  it("リミット到達では自分から開いて軸とセンサを出す", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_latched: { y_axis: ["y_axis_r_origin_sensor"] } })}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("リミット到達")).toBeInTheDocument();
    expect(screen.getByText("y_axis")).toBeInTheDocument();
    // どちらの端かはセンサ名にしか無い。軸名だけでは退避の向きを選べない
    expect(screen.getByText(/y_axis_r_origin_sensor/)).toBeInTheDocument();
    // 「異常」ではない (保護が設計どおり働いた結果であって機体の故障ではない)
    expect(screen.getByText("異常なし")).toBeInTheDocument();
  });

  it("逆向きへは動くことを書く (復帰できない軸だと読ませない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_latched: { y_axis: ["y_axis_r_origin_sensor"] } })}
      />,
    );

    expect(screen.getByText(/逆向きへは動きます/)).toBeInTheDocument();
  });

  it("ラッチ 0 件なら開いても何も出さない", () => {
    // 平常時に静かであること。0 件で出すと、本当に止まった 1 回が埋もれる
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_latched: {} })}
        defaultOpen
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.queryByText("リミット到達")).not.toBeInTheDocument();
    // **1px も占めないこと。** 空の `<ul>` が残ると、中身の無い色付きの帯が
    // 平常時にずっと出たままになる (「平常時に静かで」が崩れる)
    expect(screen.queryByRole("list", { name: "リミット到達の軸" })).not.toBeInTheDocument();
  });

  /**
   * 途絶で保護が効いていないセンサ (`LimitBlindNotice`)。`FirmwareUnconfirmedNotice` と
   * 同じ位置付け —— 「壊れている」ではなく「検出が働いていない」なので、判定チップも
   * 開閉 (`forcedOpen`) も動かさない (試合中ずっと変わらない状態であり、開きっぱなしに
   * すると安全機構のパネルごと読まれなくなる)。
   */
  it("開いたときだけ保護無効のセンサを出す (判定・開閉は動かさない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_blind_sensors: ["sub_lift_t_limit_sensor"] })}
      />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText("リミット保護 無効")).not.toBeInTheDocument();
  });

  it("開けば保護無効のセンサが見える", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_blind_sensors: ["sub_lift_t_limit_sensor"] })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText("リミット保護 無効")).toBeInTheDocument();
    expect(screen.getByText("sub_lift_t_limit_sensor")).toBeInTheDocument();
    // 「触れても止まらない」まで書く (状態だけでは次の一手を選べない)
    expect(screen.getByText(/触れても軸は止まりません/)).toBeInTheDocument();
  });

  it("保護無効 0 件なら開いても何も出さない", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ limit_blind_sensors: [] })}
        defaultOpen
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.queryByText("リミット保護 無効")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "リミット保護が無効なセンサ" }),
    ).not.toBeInTheDocument();
  });

  it("開いたときだけタスク失敗ラベルを出す (判定・開閉は動かさない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ failed_tasks: ["再励磁 (RuntimeError)"] })}
      />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText("タスク失敗")).not.toBeInTheDocument();
  });

  it("開けばタスク失敗ラベルが見える", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ failed_tasks: ["再励磁 (RuntimeError)"] })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText("タスク失敗")).toBeInTheDocument();
    expect(screen.getByText("再励磁 (RuntimeError)")).toBeInTheDocument();
  });

  it("手当てとして journal の確認を案内する (再起動を促さない)", async () => {
    const user = userEvent.setup();
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ failed_tasks: ["再励磁 (RuntimeError)"] })}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText(/journal/)).toBeInTheDocument();
    expect(screen.queryByText(/再起動して/)).not.toBeInTheDocument();
  });

  it("タスク失敗が 0 件なら開いても何も出さない", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ failed_tasks: [] })}
        defaultOpen
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.queryByText("タスク失敗")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "失敗したタスク" })).not.toBeInTheDocument();
  });

  it("開閉ボタンが開閉対象と結ばれている", async () => {
    renderWithRobot(
      <SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />,
    );

    const button = screen.getByRole("button", { expanded: false });
    await userEvent.click(button);

    const controls = button.getAttribute("aria-controls");
    expect(controls).toBeTruthy();
    expect(document.getElementById(controls as string)).not.toBeNull();
  });

  it("判定を別の要素が担う画面では、判定チップも開閉も持たない", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety()}
        showVerdict={false}
      />,
    );

    expect(screen.queryByText("異常なし")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("can_m3508")).toBeInTheDocument();
  });
});
