import { useCallback, useEffect, useRef, useState } from "react";

const RECONNECT_INTERVAL = 3000;

interface UseWebSocketReturn {
  connected: boolean;
  /** 送れたら true。切断中は false (呼び出し側が楽観的更新を止められるように) */
  send: (data: object) => boolean;
}

/**
 * WebSocket の接続・再接続・接続先切替だけを持つ。メッセージの意味は解釈しない
 * (解釈は `lib/protocol.ts`、状態遷移は `lib/robotReducer.ts`)。
 *
 * 切断中は RECONNECT_INTERVAL ごとに再接続する。会場では PC の復帰や
 * ケーブルの抜き差しで簡単に切れるため、操縦者の操作なしで戻る必要がある。
 */
export function useWebSocket(url: string, onMessage: (data: string) => void): UseWebSocketReturn {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 接続先を切り替えた後も、旧接続の close/message は非同期に届く。世代番号で弾かないと
  // 旧 URL への再接続タイマーが走り、古いサーバーの状態で画面が上書きされる
  const generationRef = useRef(0);
  // ハンドラの同一性で接続を張り直さない (再接続のたびに状態が飛ぶのを防ぐ)
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const generation = generationRef.current;
    const isCurrent = () => generation === generationRef.current;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.addEventListener("open", () => {
      if (isCurrent()) setConnected(true);
    });

    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      setConnected(false);
      reconnectTimer.current = setTimeout(connect, RECONNECT_INTERVAL);
    });

    ws.addEventListener("error", () => ws.close());

    ws.addEventListener("message", (event: MessageEvent) => {
      if (!isCurrent()) return;
      onMessageRef.current(event.data);
    });
  }, [url]);

  useEffect(() => {
    // 接続先切替の直後に旧接続の "Connected" 表示が残ると、届いていない指令を
    // 届いたものと誤認する。張り直しの間は必ず切断表示にする
    setConnected(false);
    connect();
    return () => {
      generationRef.current += 1;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  // 切断中に黙って捨てると、呼び出し側は「届いた」と区別が付かない。
  // 緊急停止のように送信の成否で画面表示を変える操作があるため結果を返す
  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(JSON.stringify(data));
    return true;
  }, []);

  return { connected, send };
}
