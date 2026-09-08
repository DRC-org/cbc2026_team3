import { useCallback, useEffect, useRef, useState } from "react";

const RECONNECT_INTERVAL = 3000;

interface UseWebSocketReturn {
  connected: boolean;
  send: (data: object) => boolean;
}

export function useWebSocket(url: string, onMessage: (data: string) => void): UseWebSocketReturn {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const generationRef = useRef(0);
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
    setConnected(false);
    connect();
    return () => {
      generationRef.current += 1;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(JSON.stringify(data));
    return true;
  }, []);

  return { connected, send };
}
