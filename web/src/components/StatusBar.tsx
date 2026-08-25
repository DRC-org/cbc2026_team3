import { useEffect, useState } from "react";

import { useRobot } from "@/context/RobotContext";

/** ステータスバーは横幅が限られるため host:port だけ出す（全体は title 属性で見せる） */
function wsHostLabel(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Clock() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return <span className="tabular-nums">{now.toLocaleTimeString("ja-JP", { hour12: false })}</span>;
}

export function StatusBar() {
  const { connected, wsUrl, openWsSettings } = useRobot();

  return (
    <div className="flex shrink-0 items-center gap-6 border-t border-line bg-base-300 px-3 py-[0.15rem] text-fg-dim">
      {/* 接続表示そのものを接続先設定の入口にする。繋がらない時に最初に見る場所なので */}
      <button
        type="button"
        onClick={openWsSettings}
        className="cursor-pointer hover:text-base-content"
        title={`接続先: ${wsUrl}（クリックで変更）`}
      >
        {connected ? (
          <>
            <span className="text-[0.75em] text-success">●</span> Connected
          </>
        ) : (
          <>
            <span className="text-[0.75em] text-error">●</span> Disconnected
          </>
        )}
        <span> {wsHostLabel(wsUrl)}</span>
      </button>

      <Clock />

      <span>
        <span className="key-hint">1</span>
        <span className="key-hint">2</span>
        <span className="key-hint">3</span>
        <span className="key-hint">4</span> タブ切替
      </span>

      <span>
        <span className="key-hint">Space</span> START / NEXT
      </span>
    </div>
  );
}
