import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useRobot } from "@/context/RobotContext";
import type { WsUrlSource } from "@/lib/wsUrl";
import { WS_URL_QUERY_KEY, normalizeWsUrl } from "@/lib/wsUrl";

const SOURCE_LABEL: Record<WsUrlSource, string> = {
  query: `URL の ?${WS_URL_QUERY_KEY}=`,
  stored: "この端末に保存",
  env: "ビルド時設定 (VITE_WS_URL)",
  origin: "配信元と同じ (既定)",
};

/** 表示中の URL からポートだけ差し替えた候補。dev サーバー経由で開いた時の直結先になる */
function directTarget(): string | null {
  return normalizeWsUrl(`${window.location.hostname}:8080`);
}

function hasQueryOverride(): boolean {
  try {
    return new URLSearchParams(window.location.search).has(WS_URL_QUERY_KEY);
  } catch {
    return false;
  }
}

/**
 * WebSocket 接続先の変更ダイアログ。
 *
 * 配信元 ≠ 制御プログラムになる構成（vite dev を Tailscale 経由で開く、
 * 配信済み UI から手元の制御 PC へ繋ぐ、予備機へ切り替える）を、
 * 再ビルドせずに現場で解決できるようにするための逃げ道。
 */
export function WsSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { wsUrl, wsUrlSource, setWsUrl, resetWsUrl, connected } = useRobot();
  const [draft, setDraft] = useState(wsUrl);
  const [error, setError] = useState<string | null>(null);

  // 開き直したときと、保存後に正規化された値へ追従させる
  useEffect(() => {
    setDraft(wsUrl);
    setError(null);
  }, [open, wsUrl]);

  const direct = directTarget();

  const apply = () => {
    if (setWsUrl(draft)) return;
    setError("接続先として解釈できません（例: drc:8080 / ws://drc:8080/ws）");
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="CONNECTION"
      ariaLabel="接続先設定"
      footer={
        <>
          <Button onClick={resetWsUrl}>既定に戻す</Button>
          <Button onClick={onClose}>閉じる</Button>
        </>
      }
    >
      <div className="flex max-w-[90vw] min-w-[26rem] flex-col gap-3">
        <div className="hstack">
          <span className="whitespace-nowrap text-fg-dim">現在</span>
          <span className="truncate">{wsUrl}</span>
          <span
            className={
              connected ? "whitespace-nowrap text-success" : "whitespace-nowrap text-error"
            }
          >
            {connected ? "● 接続中" : "● 切断"}
          </span>
        </div>
        <div className="text-fg-dim">設定元: {SOURCE_LABEL[wsUrlSource]}</div>

        <form
          className="hstack"
          onSubmit={(event) => {
            event.preventDefault();
            apply();
          }}
        >
          <label className="whitespace-nowrap" htmlFor="ws-url-input">
            接続先
          </label>
          <input
            id="ws-url-input"
            className="input min-w-0 flex-1 border-line bg-base-300 input-sm focus:border-info focus:outline-none"
            value={draft}
            spellCheck={false}
            autoComplete="off"
            placeholder="drc:8080"
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit">保存して再接続</Button>
        </form>

        {error ? <div className="text-error">{error}</div> : null}

        {direct && direct !== wsUrl ? (
          <div className="hstack">
            <span className="whitespace-nowrap text-fg-dim">候補</span>
            <Button onClick={() => setDraft(direct)}>制御 PC へ直結 ({direct})</Button>
          </div>
        ) : null}

        {hasQueryOverride() && wsUrlSource !== "query" ? (
          <div className="text-fg-dim">
            URL に ?{WS_URL_QUERY_KEY}= が付いています。リロードするとそちらが優先されます
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
