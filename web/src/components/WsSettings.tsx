import { Button, Input, Modal, ModalBody, ModalFooter, ModalHeader } from "@tsaito18/tuicss-react";
import { useEffect, useState } from "react";

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
    <Modal open={open} onClose={onClose} windowClassName="center" aria-label="接続先設定">
      <ModalHeader>CONNECTION</ModalHeader>
      <ModalBody>
        <div className="vstack" style={{ gap: "0.75rem", minWidth: "26rem", maxWidth: "90vw" }}>
          <div className="hstack" style={{ gap: "0.5rem" }}>
            <span className="dim nowrap">現在</span>
            <span className="ellipsis">{wsUrl}</span>
            <span className={connected ? "success-text nowrap" : "danger-text nowrap"}>
              {connected ? "● 接続中" : "● 切断"}
            </span>
          </div>
          <div className="dim">設定元: {SOURCE_LABEL[wsUrlSource]}</div>

          <form
            className="hstack"
            style={{ gap: "0.5rem" }}
            onSubmit={(event) => {
              event.preventDefault();
              apply();
            }}
          >
            <label className="nowrap" htmlFor="ws-url-input">
              接続先
            </label>
            <Input
              id="ws-url-input"
              className="spacer"
              value={draft}
              spellCheck={false}
              autoComplete="off"
              placeholder="drc:8080"
              onChange={(event) => setDraft(event.target.value)}
            />
            <Button type="submit">保存して再接続</Button>
          </form>

          {error ? <div className="danger-text">{error}</div> : null}

          {direct && direct !== wsUrl ? (
            <div className="hstack" style={{ gap: "0.5rem" }}>
              <span className="dim nowrap">候補</span>
              <Button type="button" onClick={() => setDraft(direct)}>
                制御 PC へ直結 ({direct})
              </Button>
            </div>
          ) : null}

          {hasQueryOverride() && wsUrlSource !== "query" ? (
            <div className="dim">
              URL に ?{WS_URL_QUERY_KEY}= が付いています。リロードするとそちらが優先されます
            </div>
          ) : null}
        </div>
      </ModalBody>
      <ModalFooter>
        <Button type="button" onClick={resetWsUrl}>
          既定に戻す
        </Button>
        <Button type="button" onClick={onClose}>
          閉じる
        </Button>
      </ModalFooter>
    </Modal>
  );
}
