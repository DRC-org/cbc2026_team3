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
    <div className="tui-statusbar">
      <ul>
        <li>
          {/* 接続表示そのものを接続先設定の入口にする。繋がらない時に最初に見る場所なので */}
          <button
            type="button"
            onClick={openWsSettings}
            style={{ cursor: "pointer" }}
            title={`接続先: ${wsUrl}（クリックで変更）`}
          >
            {connected ? (
              <>
                <span className="symbol success-text">●</span> Connected
              </>
            ) : (
              <>
                <span className="symbol danger-text">●</span> Disconnected
              </>
            )}
            <span className="dim"> {wsHostLabel(wsUrl)}</span>
          </button>
        </li>
        <li>
          <Clock />
        </li>
        <li>
          <span className="key-hint">1</span>
          <span className="key-hint">2</span>
          <span className="key-hint">3</span>
          <span className="key-hint">4</span> タブ切替
        </li>
        <li>
          <span className="key-hint">Space</span> START / NEXT
        </li>
      </ul>
    </div>
  );
}
