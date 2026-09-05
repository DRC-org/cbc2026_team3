import { useState } from "react";

import { MotorDetail } from "@/components/tuning/MotorDetail";
import { ResponsePanel } from "@/components/tuning/ResponsePanel";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { tempThresholdsOf } from "@/lib/healthVerdict";
import { isDuringMatch } from "@/lib/phase";
import { NO_PC_SIDE_PID_NOTE, PID_PARAMS, entryKey } from "@/lib/pidTuning";
import type { PidKey, TuningEntry } from "@/lib/pidTuning";
import { tuningKey } from "@/lib/robotReducer";
import { ROBOTS } from "@/lib/robots";

interface Selection {
  robot: string;
  motor: string;
}

/**
 * PID 調整タブ。マスタ・ディテール構成。
 *
 * 以前は両機の全モータを縦に展開しており、1 基を触るだけでもスクロールが要り、
 * 「今どのモータを見ているのか」が視界から外れていた。調整は 1 基ずつ応答を見ながら
 * 詰める作業なので、左で対象を選び、右をその 1 基に明け渡す形にする。
 *
 * **表示する値の出どころはサーバーの `motors[].pid` ただ 1 つ。** 以前はここを持たず
 * 0 で初期化しており、開いた直後の `0.00` をそのまま送ると全ゲインが 0 になって
 * 位置制御ループが無効化された。操縦者には config を読む以外に元の値へ戻す術が無い。
 */
export function MotorTuning() {
  const states = useRobotStates();
  const { matchState, connected, eStopActive, serverInfo, tuningCaptures } = useRobotStatus();
  const { send } = useRobotCommands();
  const [values, setValues] = useState<Record<string, Partial<Record<PidKey, number>>>>({});
  const [selected, setSelected] = useState<Selection | null>(null);

  // 届いているモータの総数。「まだ何も来ていない」と「来ているが調整対象が無い」は
  // 操縦者の次の行動が違う (待つ / この画面では触れないと理解する)
  const motorCount = ROBOTS.reduce(
    (n, { key }) => n + Object.keys(states[key]?.motors ?? {}).length,
    0,
  );

  // PC 側 PID を持つモータだけが調整対象。判定はサーバーの `pid` の有無だけで行い、
  // ドライバ種別を UI へ書き写さない。欠けた配信は「調整不可」へ倒す
  // (調整できる側へ倒すと、値を持たないまま送る = 0 上書きの経路が戻る)
  const entries: TuningEntry[] = ROBOTS.flatMap(({ key, label }) =>
    Object.entries(states[key]?.motors ?? {}).flatMap(([motor, motorState]) => {
      const pid = motorState.pid ?? null;
      return pid === null ? [] : [{ robot: key, robotLabel: label, motor, motorState, pid }];
    }),
  );

  // 初回描画時にはまだモータが届いていない。届いた時点で先頭を自動選択する
  const active =
    entries.find((e) => selected && e.robot === selected.robot && e.motor === selected.motor) ??
    entries[0];

  // 未編集の項目はサーバーが配っている現在値をそのまま出す。ここを 0 で埋めると
  // 画面の表示と機体の実際のゲインが食い違う
  const getValue = (entry: TuningEntry, param: PidKey) =>
    values[entryKey(entry)]?.[param] ?? entry.pid[param];

  const setValue = (entry: TuningEntry, param: PidKey, val: number) => {
    const key = entryKey(entry);
    setValues((prev) => ({ ...prev, [key]: { ...prev[key], [param]: val } }));
  };

  /** 編集を捨てて配信値へ戻す。**送信の成否に関わらず、表示の正はサーバー配信** */
  const discardEdits = (entry: TuningEntry) => {
    const key = entryKey(entry);
    setValues((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _dropped, ...rest } = prev;
      return rest;
    });
  };

  // 3 項目を個別に送ると PID が中途半端に混ざった状態が制御周期をまたいで残り、
  // 通らないときの拒否も 3 通になる。1 通にまとめてサーバーへ渡す。
  //
  // **送れたら編集値を捨てて配信へ再同期する。** 残すと、そのモータで実際に
  // 効いているゲインが画面のどこにも出なくなる (サーバーに拒否されても編集値を
  // 出し続ける)。かつて「画面に元の値がどこにも無い」状態で 0 上書き事故が
  // 起きており、編集後にそれが再現していた。送れなかったときは捨てない ——
  // 機体には何も届いていないので、編集はまだ操縦者のものである
  const sendAll = (entry: TuningEntry) => {
    const gains = Object.fromEntries(PID_PARAMS.map(({ key }) => [key, getValue(entry, key)]));
    if (send({ type: "set_param", motor: entry.motor, gains })) discardEdits(entry);
  };

  // 試合中の set_param はサーバーが拒否する (走行中の位置制御ループの特性が変わり、
  // 同期グループ全体に適用されるため直結した左右軸が負荷下で同時に別特性になる)。
  // 緊急停止中も同じく拒否される (`lib/commands.py` の allowed_during_e_stop=False)。
  // 判定の正はサーバーで、ここは押す前に理由を出すだけ。拒否トーストで気付くのでは遅い
  const blockedReason = !connected
    ? "切断中のため送信できません"
    : eStopActive
      ? "緊急停止中はパラメータを変更できません"
      : isDuringMatch(matchState.phase)
        ? "試合中はパラメータを変更できません"
        : null;

  if (entries.length === 0) {
    return (
      <Page className="flex flex-col items-center justify-center">
        <Panel legend="PID TUNING" className="flex-none">
          {motorCount === 0 ? (
            <p className="text-base-content/70">モータ情報なし — 接続待機中...</p>
          ) : (
            <div className="flex flex-col gap-1">
              <p className="text-base-content/70">調整対象のモータがありません</p>
              <p className="text-[0.9em] text-base-content/60">{NO_PC_SIDE_PID_NOTE}</p>
            </div>
          )}
        </Panel>
      </Page>
    );
  }

  const captures = active ? (tuningCaptures[tuningKey(active.robot, active.motor)] ?? []) : [];

  return (
    <Page className="grid grid-cols-[minmax(13rem,18rem)_minmax(0,1fr)]">
      <Panel legend="モータ" bodyClassName="p-0">
        <div className="scroll flex min-h-0 flex-1 flex-col">
          {ROBOTS.map(({ key, label }) => {
            const group = entries.filter((e) => e.robot === key);
            if (group.length === 0) return null;
            return (
              <div key={key} className="flex flex-col">
                <div className="sticky top-0 border-b border-base-300 bg-base-200 px-2 py-[0.15rem] text-[0.85em] text-base-content/70">
                  {label}
                </div>
                {group.map((entry) => {
                  const isActive = active?.robot === key && active?.motor === entry.motor;
                  return (
                    <button
                      key={entry.motor}
                      type="button"
                      onClick={() => setSelected({ robot: key, motor: entry.motor })}
                      aria-current={isActive ? "true" : undefined}
                      className={cx(
                        "flex cursor-pointer items-center gap-2 border-l-2 border-transparent px-2 py-[0.3rem] text-left hover:bg-base-200",
                        isActive && "border-l-info bg-base-200 font-medium",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{entry.motor}</span>
                      {/* 温度を測れないドライバは「—」。単位は値があるときだけ付ける */}
                      <span className="shrink-0 font-mono text-[0.85em] text-base-content/60 tabular-nums">
                        {entry.motorState.temp === null
                          ? "—"
                          : `${entry.motorState.temp.toFixed(0)}℃`}
                      </span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
        {/* 一覧に出ていないモータの説明。絞り込みの理由が画面から読めないと、
            操縦者は「表示されないモータが壊れている」と読む */}
        <p className="border-t border-base-300 px-2 py-1.5 text-[0.8em] leading-snug text-base-content/60">
          {NO_PC_SIDE_PID_NOTE}
        </p>
      </Panel>

      {active ? (
        <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2">
          <MotorDetail
            key={entryKey(active)}
            entry={active}
            getValue={getValue}
            setValue={setValue}
            onSend={() => sendAll(active)}
            edited={entryKey(active) in values}
            onDiscard={() => discardEdits(active)}
            blockedReason={blockedReason}
            tempThresholds={tempThresholdsOf(serverInfo)}
          />
          <ResponsePanel motor={active.motor} captures={captures} />
        </div>
      ) : null}
    </Page>
  );
}
