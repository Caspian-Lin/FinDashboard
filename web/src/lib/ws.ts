import { useEffect, useState } from "react";

export interface WsEvent {
  type: string;
  [key: string]: unknown;
}

export function useWebSocket(onEvent?: (e: WsEvent) => void) {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/events`);

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as WsEvent;
        onEvent?.(data);
      } catch {
        // ignore
      }
    };

    return () => ws.close();
  }, []);

  return connected;
}
