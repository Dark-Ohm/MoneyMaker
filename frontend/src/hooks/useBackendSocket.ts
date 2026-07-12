import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

type ConnectionState = "connecting" | "connected" | "disconnected";

export interface TickData {
  timestamp: string;
  market: string;
  symbol: string;
  price: number;
  volume_24h: number;
  change_pct: number;
  extra: Record<string, unknown>;
}

export interface LogEntry {
  timestamp: string;
  type: string;
  data: Record<string, unknown>;
}

export interface BackendStatus {
  sidecar: string;
  port: number;
  db_path: string;
  connected_clients: number;
}

interface UseBackendSocketReturn {
  ticks: TickData[];
  logs: LogEntry[];
  status: BackendStatus | null;
  connectionState: ConnectionState;
  lastPingMs: number | null;
}

const WS_BASE = "ws://127.0.0.1";
const MAX_TICKS = 200;
const MAX_LOGS = 100;
const PING_INTERVAL_MS = 5000;

async function getSidecarPort(): Promise<number> {
  try {
    return await invoke<number>("get_sidecar_port");
  } catch {
    // Dev mode fallback: try URL param or localStorage
    const url = new URL(window.location.href);
    const port = url.searchParams.get("port");
    if (port) return parseInt(port, 10);
    return parseInt(localStorage.getItem("backend_port") || "0", 10);
  }
}

export function useBackendSocket(): UseBackendSocketReturn {
  const [ticks, setTicks] = useState<TickData[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [lastPingMs, setLastPingMs] = useState<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const portRef = useRef<number>(0);

  const connect = useCallback(async () => {
    // Get port from Tauri command or fallback
    const port = await getSidecarPort();
    portRef.current = port;

    if (!port || port === 0) {
      setConnectionState("disconnected");
      // Retry in 2s if port not ready yet
      reconnectTimerRef.current = setTimeout(connect, 2000);
      return;
    }

    setConnectionState("connecting");
    const ws = new WebSocket(`${WS_BASE}:${port}/ws`);

    ws.onopen = () => {
      setConnectionState("connected");
      wsRef.current = ws;

      // Fetch initial status
      fetch(`http://127.0.0.1:${port}/api/status`)
        .then((r) => r.json())
        .then((s: BackendStatus) => setStatus(s))
        .catch(() => {});

      // Start ping
      let pingTs = 0;
      pingTimerRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          pingTs = Date.now();
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, PING_INTERVAL_MS);

      // Capture pong for latency
      const origOnMessage = ws.onmessage;
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "pong" && pingTs > 0) {
            setLastPingMs(Date.now() - pingTs);
          }
        } catch {
          // ignore
        }
        if (origOnMessage) origOnMessage.call(ws, ev);
      };
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
          case "tick":
            setTicks((prev) => [msg.data as TickData, ...prev].slice(0, MAX_TICKS));
            break;
          case "signal":
          case "log":
          case "status":
          case "error":
            setLogs((prev) => [{ timestamp: msg.data.timestamp || new Date().toISOString(), type: msg.type, data: msg.data }, ...prev].slice(0, MAX_LOGS));
            break;
        }
      } catch {
        // malformed message, ignore
      }
    };

    ws.onclose = () => {
      setConnectionState("disconnected");
      wsRef.current = null;
      if (pingTimerRef.current) {
        clearInterval(pingTimerRef.current);
        pingTimerRef.current = null;
      }
      // Reconnect after 3s
      reconnectTimerRef.current = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    connect();

    return () => {
      if (pingTimerRef.current) clearInterval(pingTimerRef.current);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { ticks, logs, status, connectionState, lastPingMs };
}
