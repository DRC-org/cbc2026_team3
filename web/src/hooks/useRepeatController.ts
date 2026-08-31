import { useCallback, useEffect, useRef, useState } from "react";

/** 押し始めてから連続発火に入るまでの待ち [ms]。単発で離す操作を吸わない長さ */
export const HOLD_DELAY_MS = 400;
/** 連続発火の間隔 [ms] */
export const HOLD_INTERVAL_MS = 150;
/** 何回発火するごとに 1 回あたりの実効量を倍にするか */
export const HOLD_ACCEL_EVERY = 6;

export interface RepeatController {
  start: () => void;
  stop: () => void;
  /** 現在の実効倍率。表示用で、離すと 1 に戻る */
  multiplier: number;
}

/**
 * 「押している間くり返す」の発火制御。ポインタ (`useHoldRepeat`) と
 * キーボード (`useHoldKey`) が同じ engine を共有するために切り出してある。
 *
 * **発火の間隔ではなく 1 回あたりの量を伸ばす。** 可動範囲 370mm を刻み 10mm で
 * 渡るには 37 回の指令が要る。間隔を詰めて速くすると、そのぶん CAN 上の
 * `SET_TARGET` が増え、同じ時間に流れるフレーム数が押しっぱなしの間だけ跳ね上がる。
 * 量を伸ばせば発火回数は据え置きのまま距離が伸びる。
 *
 * **倍率は押下ごとに 1 へ戻す。** 前回の押下の勢いが残っていると、次に軽く 1 回
 * 押したつもりが大きく動く。押した瞬間は常に選択した刻みちょうどで出る。
 */
export function useRepeatController(
  fire: (multiplier: number) => void,
  enabled: boolean,
  maxMultiplier = 1,
): RepeatController {
  const fireRef = useRef(fire);
  fireRef.current = fire;
  const maxRef = useRef(maxMultiplier);
  maxRef.current = maxMultiplier;

  const delayRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const countRef = useRef(0);

  const [multiplier, setMultiplier] = useState(1);

  const stop = useCallback(() => {
    if (delayRef.current !== null) {
      clearTimeout(delayRef.current);
      delayRef.current = null;
    }
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    countRef.current = 0;
    setMultiplier(1);
  }, []);

  /** 発火 1 回。実効倍率は「何回目か」だけで決まるので、経路によらず同じ伸び方になる */
  const tick = useCallback(() => {
    const step = 2 ** Math.floor(countRef.current / HOLD_ACCEL_EVERY);
    const capped = Math.max(1, Math.min(step, maxRef.current));
    countRef.current += 1;
    setMultiplier(capped);
    fireRef.current(capped);
  }, []);

  const start = useCallback(() => {
    if (!enabled) return;
    // 押し直しで interval が積み上がると 1 回の押下が 2 倍の速さで走る
    stop();
    // 最初の 1 回は押した瞬間に出す。単発の操作が待たされないようにする
    tick();
    delayRef.current = setTimeout(() => {
      delayRef.current = null;
      intervalRef.current = setInterval(tick, HOLD_INTERVAL_MS);
    }, HOLD_DELAY_MS);
  }, [enabled, stop, tick]);

  // アンマウントでも必ず止める。タブを切り替えただけで送り続けないため
  useEffect(() => stop, [stop]);

  // 押している最中に操作が塞がれたら (緊急停止・切断・モード離脱) その場で止める。
  // enabled を start の入口でしか見ないと、押し始めたときに許されていた発火が
  // 塞がれた後も指を離すまで続く
  useEffect(() => {
    if (!enabled) stop();
  }, [enabled, stop]);

  return { start, stop, multiplier };
}
