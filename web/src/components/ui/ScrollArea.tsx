import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cx } from "@/lib/cx";

/**
 * 端に「まだ続きがある」ことを出すスクロール面。**その向きに続きがあるあいだだけ**
 * 出す —— 常に出すと「ここで終わり」が読めなくなる (項目の少ないベンチ設定は
 * 一度も溢れない)。
 *
 * **スクロールバーには任せられない。** 検証に使った環境では
 * `offsetWidth - clientWidth` が 0 (静止中は 1px も描かれないオーバーレイ) で、
 * 従来型のバーが出る環境でも「区分が丸ごと画面の外にある」ことまでは読めない。
 * 半分に切れた見出しだけが手がかりになり、それは描画の崩れとも読める。
 *
 * **上端も要る。** 自動スクロール (指差喚呼の「次」・シーケンスの現在ステップ) は
 * 操縦者が何も動かさなくても起きるので、切れた行が上端に現れたときこそ崩れに見える。
 *
 * **地の色へのフェードにしてはならない。** 白へ溶かすと切れかけた要素ごと消えて
 * 逆に「ここで終わり」に見える (実描画で確かめた)。縁が落とす影として描く。
 *
 * 測り直す契機は 描画 / スクロール / ウィンドウのリサイズ の 3 つ。子が自分の state
 * だけで高さを変えた直後 (`MotorCheckPanel` の開閉) は測り直さないので、下端の合図が
 * 1 手ぶん遅れる —— 次のスクロールかチェックで揃う。縮む側は scrollTop が詰められた
 * ときにスクロールが起きるので揃ったままで、嘘の合図は残らない。
 *
 * **`ui/` に置いてあるのは、溢れが画面に出ない面が 1 つではないため。** 実測 (1366x768):
 *
 * | 面 | 本文 | 中身 |
 * |---|---|---|
 * | Monitor 準備中の指差喚呼 | 479px | 1252px |
 * | Monitor 準備中の機体状態 (2 機) | 526px | 1913px |
 *
 * 片方だけ直すと、同じ壊れ方が残っている面と直った面が同じ画面に並ぶ。
 */
export function ScrollArea({ className, children }: { className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ above: false, below: false });

  // 中身の高さは項目のチェックでも動作確認パネルの開閉でも変わるので毎描画で測り直す。
  // 判定が変わらなければ setState は再描画を起こさない
  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    // 端で丸め誤差のぶん出っぱなしにならないよう 1px の余裕を持たせる
    const above = el.scrollTop > 1;
    const below = el.scrollTop + el.clientHeight < el.scrollHeight - 1;
    setEdges((prev) => (prev.above === above && prev.below === below ? prev : { above, below }));
  }, []);
  useEffect(measure);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={ref}
        onScroll={measure}
        className={cx("scroll flex min-h-0 flex-1 flex-col", className)}
      >
        {children}
      </div>
      {edges.above ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-4 bg-linear-to-b from-base-content/18 to-transparent"
        />
      ) : null}
      {edges.below ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-0 h-4 bg-linear-to-t from-base-content/18 to-transparent"
        />
      ) : null}
    </div>
  );
}
