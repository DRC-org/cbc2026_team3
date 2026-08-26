import { useEffect, useState } from "react";

import { Kbd } from "@/components/ui/Kbd";
import { useRobot } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { TONE_STATUS_CLASS } from "@/lib/tone";

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

  return (
    <span className="font-mono tabular-nums">
      {now.toLocaleTimeString("ja-JP", { hour12: false })}
    </span>
  );
}

export function StatusBar() {
  const { connected, wsUrl, openWsSettings } = useRobot();

  return (
    <div className="flex shrink-0 items-center gap-4 border-t border-base-300 bg-base-100 px-2 py-[0.1rem] text-[0.82em] text-base-content/70">
      {/* 接続表示そのものを接続先設定の入口にする。繋がらない時に最初に見る場所なので */}
      <button
        type="button"
        onClick={openWsSettings}
        className="flex cursor-pointer items-center gap-1.5 hover:text-base-content"
        title={`接続先: ${wsUrl}（クリックで変更）`}
      >
        <span className={cx(TONE_STATUS_CLASS[connected ? "success" : "error"], "status-sm")} />
        {connected ? "Connected" : "Disconnected"}
        <span className="font-mono">{wsHostLabel(wsUrl)}</span>
      </button>

      <Clock />

      <span className="flex items-center gap-1">
        <Kbd>1</Kbd>
        <Kbd>2</Kbd>
        <Kbd>3</Kbd>
        <Kbd>4</Kbd>
        タブ切替
      </span>

      <span className="flex items-center gap-1">
        <Kbd>Space</Kbd> START / NEXT
      </span>
    </div>
  );
}
