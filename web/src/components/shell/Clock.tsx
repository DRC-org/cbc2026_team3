import { useEffect, useState } from "react";

/**
 * ヘッダーに出す現在時刻。**独立したコンポーネントであることが本体で、飾りではない。**
 *
 * 毎秒 `setState` するので、ヘッダー本体へインライン展開すると 1 秒ごとに
 * ヘッダー全体（タブ帯を含む）が描き直される。ここで区切っておくと、
 * 毎秒動くのはこの `<span>` だけになる。
 */
export function Clock() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <span className="font-mono tabular-nums">
      {now.toLocaleTimeString("ja-JP", { hour12: false })}
    </span>
  );
}
