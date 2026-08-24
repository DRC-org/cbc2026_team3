import { vi } from "vitest";

type Listener = (event: unknown) => void;

/**
 * useRobotSocket 用の WebSocket スタブ。
 *
 * jsdom は WebSocket を実装しているが実際に接続を張ってしまうため、
 * テストからサーバー側イベント (open/message/close) を任意の順で発火させられる
 * 差し替え実装を用意する。
 */
export class MockWebSocket {
  static instances: MockWebSocket[] = [];

  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState: number = MockWebSocket.CONNECTING;
  readonly url: string;
  readonly sent: string[] = [];
  private listeners: Record<string, Listener[]> = {};

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    (this.listeners[type] ??= []).push(listener);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners[type] = (this.listeners[type] ?? []).filter((l) => l !== listener);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.emit("close", {});
  }

  /** サーバーが接続を受理した状態にする */
  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.emit("open", {});
  }

  /** サーバーからの JSON メッセージ受信を模す */
  receive(payload: unknown): void {
    this.emit("message", { data: typeof payload === "string" ? payload : JSON.stringify(payload) });
  }

  /** 送信済みペイロードを JSON として取り出す */
  sentJson(): unknown[] {
    return this.sent.map((s) => JSON.parse(s));
  }

  private emit(type: string, event: unknown): void {
    for (const listener of this.listeners[type] ?? []) listener(event);
  }
}

export function installMockWebSocket(): typeof MockWebSocket {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  return MockWebSocket;
}

export function latestSocket(): MockWebSocket {
  const socket = MockWebSocket.instances.at(-1);
  if (!socket) throw new Error("WebSocket が生成されていません");
  return socket;
}
