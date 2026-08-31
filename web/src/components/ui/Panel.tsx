import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BORDER_L_CLASS } from "@/lib/tone";

interface PanelProps {
  legend?: ReactNode;
  /** 見出し行の右端に置く操作。本文へ置くと 1 行ぶん縦を余計に食う */
  actions?: ReactNode;
  /**
   * 左端を状態色で塗る。周辺視野でも状態の変化に気付けるようにするためのもので、
   * 状態を持たないパネルでは省く。
   *
   * **クラスは `TONE_BORDER_L_CLASS` のリテラルから引く。** `border-` を実行時に
   * `border-l-` へ置換するような組み立て方をすると Tailwind の走査から漏れ、
   * CSS ごと出力されない (= 色が付かない)。
   */
  accentTone?: Tone;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

/** アクセントバーの太さ。パネルをまたいで揃える */
const ACCENT_WIDTH_CLASS = "border-l-[0.4rem]";

/**
 * 画面上の枠を持つ唯一の単位。daisyUI の `card` + `card-border` で描く。
 * 枠は「パネル 1 つにつき 1 本」で入れ子にしない（内部の区切りは Section が担う）。
 *
 * `card-body` は使わない。既定の padding が 1.5rem とこの画面には過大で、
 * `card-xs` 等のサイズ修飾子は同時に font-size まで固定してしまい、
 * ルートの `clamp()` による全体スケーリングから本文だけが外れるため。
 */
export function Panel({
  legend,
  actions,
  accentTone,
  className,
  bodyClassName,
  children,
}: PanelProps) {
  return (
    <section
      className={cx(
        "card card-border flex min-h-0 min-w-0 flex-col border-base-300 bg-base-100",
        accentTone && ACCENT_WIDTH_CLASS,
        accentTone && TONE_BORDER_L_CLASS[accentTone],
        className,
      )}
    >
      {legend === undefined ? null : (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-base-300 px-2 py-[0.15rem]">
          <h2 className="min-w-0 truncate text-[0.82em] font-medium tracking-wide text-base-content/70">
            {legend}
          </h2>
          {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
        </div>
      )}
      <div className={cx("flex min-h-0 flex-1 flex-col p-2", bodyClassName)}>{children}</div>
    </section>
  );
}
