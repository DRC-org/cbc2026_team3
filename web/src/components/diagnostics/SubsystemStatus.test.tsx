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

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    unenergized_motors: [],
    firmware_unconfirmed_motors: [],
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
    // 試合中の操縦者が画面へ視線を戻すのは一瞬しかない。平常時は静かに保つ
    renderWithRobot(
      <SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />,
    );

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/同期ずれ/)).not.toBeInTheDocument();
  });

  it("defaultOpen が後から真になったら開く (再マウントされないので追従が要る)", () => {
    // `RobotControl` は手動操縦へ切り替わったときに `defaultOpen` を false → true で
    // 渡し直すが、この部品は grid の同じ位置・同じ型のまま残るので**再マウント
    // されない**。初期値としてしか使わないと、試合中に手動へ入っても畳まれたまま、
    // しかもパネルだけが列の全高へ伸びた白い箱になる ——
    // 機体を直接動かしている最中に診断が閉じたままになる
    // **Provider ごと再描画する。** `renderWithRobot` の rerender へ素の要素を
    // 渡すとルートの型が変わって**再マウント**され、useState の初期値が使われる
    // ので、追従が無くても緑になってしまう (実際の画面では再マウントされない)
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

  /**
   * 原点スイッチの反応 (`config/checklist.yaml` の `origin_sensor_react`) を
   * 画面から確かめる唯一の場所。**モータ一覧とは別に描く** —— サーバーも
   * `sensors:` を `motors:` と別セクションに持っている (モータ一覧に
   * 「常に 0 のモータ」を並べないため)。
   */
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
    // モータ基数はセンサを数えない (混ぜると「常に 0 のモータ」が 1 基増えて見える)
    expect(screen.getByText(/モータ 1$/)).toBeInTheDocument();
  });

  it("同期ずれラッチは畳んだ状態を上書きして開き、復旧手順まで出す", () => {
    // 緊急停止を解除してもその軸は動かない。解除操作だけを繰り返させてはならない
    renderWithRobot(
      <SubsystemStatus
        connected
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
      // 同期ずれラッチの行にまで再励磁ボタンが付くと、押しても直らないボタンになる
      renderWithRobot(
        <SubsystemStatus
          connected
          health={HEALTH}
          motors={MOTORS}
          safety={safety({ sync_violations: ["y_axis"] })}
          onReenergize={() => {}}
        />,
      );

      expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "再励磁" })).not.toBeInTheDocument();
    });

    it("処理中はサーバーの配信どおり押せなくなる", () => {
      // 押した後の 0.1〜1.5 秒は unenergized_motors が消えないので、これが無いと
      // 操縦者は「押しても何も起きない」と読んで 2 回目を押す (そして拒否される)
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
    // WS は繋がったままモータ状態も届き続けるので、ここに出さないと誰も気付けない
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

    expect(screen.getByText("位置制御ループ停止")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("目標値再送の停止を自分から主張する", () => {
    // 20Hz の再送が止まると 500ms 後にファーム側ウォッチドッグが効き、
    // グリッパ・コンベア・壁が無反応になる。WS は繋がったままなので画面からは原因が分からない
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

    expect(screen.getByText("目標値再送停止")).toBeInTheDocument();
    expect(screen.getByText("gripper, conveyor")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("異常中は操縦者が畳もうとしても畳めない", async () => {
    // 「自分から開く」だけでは足りない。試合中に一度畳めてしまえば、
    // その後に出た異常も畳んだままになり、見逃しの経路がそのまま残る
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
    expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
  });

  it("異常中に畳もうとした操作は、解消した時点で効く", async () => {
    // 強制開示のまま操作を握り潰すと、異常が消えた後も 32 個の数字が
    // 試合の残り時間ずっと開いたままになる (平常時に静かでなくなる)
    // rerender で異常の解消を再現するため、ここは素の render を使う
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
    // 開く条件を error だけに絞ると、焼損に向かう温度上昇を畳んだまま見逃す。
    // 過熱の判定はサーバー (config の temp_warning_c) が持ち、warning として届く
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

  /**
   * 温度の色分けの境界はサーバーの config だけが持つ。呼び出し元 → SubsystemStatus →
   * MotorSummary → MotorStatus の受け渡しが 1 段でも切れると、config を変えても
   * 画面の色だけが変わらない (数値は出続けるので画面からは気付けない)。
   */
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
    // 強制開示は異常時だけ。平常時まで開きっぱなしにすると数字の海に戻る
    renderWithRobot(
      <SubsystemStatus connected health={HEALTH} motors={MOTORS} safety={safety()} />,
    );

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

  /**
   * CAN 途絶がワーク落下に繋がりうるバスの通知 (`WorkpieceRiskNotice`)。
   *
   * バスは既に復旧して `state: "ok"` (= 判定チップは「異常なし」のまま) でも、
   * このバスに乗っている電磁弁が吸着中のワークを落とした可能性は消えない。
   * `evaluateHealth` の判定 (見出しチップ) は動かさず、別の主張として出す。
   */
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

    // 見出しの判定チップは変えない (BusHealth.state の判定そのものには触れない)
    expect(screen.getByText("異常なし")).toBeInTheDocument();
    // それでも自分から開いて主張する
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText(/CAN 途絶 2回/)).toBeInTheDocument();
    // バス名は通知欄と診断テーブルの両方に出るので複数ヒットする
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

  it("エピソード 0 件なら、ワーク落下しうるバスでも何も出さない", () => {
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
  });

  /**
   * `INFO` 未受信の通知 (`FirmwareUnconfirmedNotice`)。
   *
   * 「確認できていない」であって「壊れている」ではないので、判定チップ (見出し) も
   * 開閉 (`forcedOpen`) も動かさない —— `_info` は一度受ければ二度と外れないラッチ
   * なので、猶予を過ぎても残るのは大半が試合中ずっと変わらない状態であり、
   * ワーク落下のような 1 事象ではない。開いたときに見える情報として畳める。
   */
  it("開いたときだけ INFO 未確認のモータを出す (判定・開閉は動かさない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ firmware_unconfirmed_motors: ["gripper"] })}
      />,
    );

    // 見出しの判定チップは変えない (「異常」ではないため)
    expect(screen.getByText("異常なし")).toBeInTheDocument();
    // 自分から開かせない (畳んだままにできる)
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

  /**
   * この状態が起きる最も現実的なきっかけは「1 枚の基板が丸ごと `INFO` を出していない」
   * なので、電磁弁 6ch のように複数が同時に並ぶ。1 行へ `join(", ")` すると
   * `truncate` で途中から読めなくなり、操縦者は指差喚呼 (`firmware_match`) に
   * 答えられない。**モータごとに 1 行**にする。
   */
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
      // 1 行にまとめていると "valve_1, valve_2, ..." という 1 つのノードになり、
      // 名前 1 個での完全一致は取れない
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  /**
   * **手当ての文面は「電源・CAN 配線」であってはならない。** サーバーは `FEEDBACK` が
   * 届いているモータだけをここへ載せる (`_firmware_unconfirmed_motors` の STALE 除外)
   * ので、配線を見ても必ず何も見つからない。直すべきはファーム側の `INFO` 送信経路。
   */
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
  });

  /**
   * 投げっぱなしタスクの失敗ラベル (`FailedTasksNotice`)。`FirmwareUnconfirmedNotice`
   * と同じ位置付け —— 「異常」ではないので判定チップ (見出し) も開閉 (`forcedOpen`)
   * も動かさない。1 度失敗した記録が試合開始まで残るだけで、機体が今も壊れていると
   * は限らない。
   */
  it("開いたときだけタスク失敗ラベルを出す (判定・開閉は動かさない)", () => {
    renderWithRobot(
      <SubsystemStatus
        connected
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ failed_tasks: ["再励磁 (RuntimeError)"] })}
      />,
    );

    // 見出しの判定チップは変えない (「異常」ではないため)
    expect(screen.getByText("異常なし")).toBeInTheDocument();
    // 自分から開かせない (畳んだままにできる)
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

  /**
   * **手当ての文面は再起動を促してはならない。** これは投げっぱなしタスクの内部例外
   * (トレースバックは journal にしか無い) であり、再起動で直る保証は無い。
   */
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
    // 「再起動ではなく journal を見よ」という否定形は許す。「再起動してください」の
    // ような行動喚起だけを弾く (文言に「再起動」という字面が 1 文字も出ない、
    // ではなく「再起動を促していない」ことを見る)
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
  });

  it("開閉ボタンが開閉対象と結ばれている", async () => {
    // aria-expanded だけでは「何が開くのか」が読み上げに伝わらない
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
    // Monitor の準備画面は StartGate が「異常があるか」を最大の要素で答える。
    // 同じ文字列をこの見出しにも出すと、同じ事実が同じ画面に 2 回並ぶ
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
    // 中身 (どのバス・どのモータか) は常に見えている
    expect(screen.getByText("can_m3508")).toBeInTheDocument();
  });
});
