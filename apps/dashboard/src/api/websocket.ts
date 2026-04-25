// CCDT WebSocket Manager
// Manages a single persistent WebSocket connection for topology streaming.

type Listener = (data: unknown) => void;

class WebSocketManager {
  private ws:        WebSocket | null = null;
  private url:       string;
  private listeners: Set<Listener>   = new Set();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 2000;
  private shouldConnect  = false;

  constructor(url: string) {
    this.url = url;
  }

  connect() {
    this.shouldConnect = true;
    this._connect();
  }

  private _connect() {
    if (!this.shouldConnect) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    try {
      // Use relative ws path so Vite proxy handles it in dev
      const wsUrl =
        this.url.startsWith('ws')
          ? this.url
          : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${this.url}`;

      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        this.reconnectDelay = 2000;
      };

      this.ws.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data as string);
          this.listeners.forEach(l => l(data));
        } catch {
          // ignore parse errors
        }
      };

      this.ws.onclose = () => {
        if (!this.shouldConnect) return;
        this.reconnectTimer = setTimeout(() => {
          this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 30_000);
          this._connect();
        }, this.reconnectDelay);
      };

      this.ws.onerror = () => {
        this.ws?.close();
      };
    } catch {
      // WebSocket not available (SSR / test)
    }
  }

  disconnect() {
    this.shouldConnect = false;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }

  addListener(fn: Listener) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  get isConnected() {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

// Singleton instances
export const topologyWS  = new WebSocketManager('/ws/topology/stream');
export const ebpfWS      = new WebSocketManager('/ws/ebpf/stream');

export default WebSocketManager;
