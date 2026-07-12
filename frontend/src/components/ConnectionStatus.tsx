import type { BackendStatus } from "../hooks/useBackendSocket";

interface ConnectionStatusProps {
  state: "connecting" | "connected" | "disconnected";
  lastPingMs: number | null;
  backendStatus: BackendStatus | null;
}

export function ConnectionStatus({ state, lastPingMs, backendStatus }: ConnectionStatusProps) {
  const stateLabel =
    state === "connected"
      ? "Connected"
      : state === "connecting"
        ? "Connecting..."
        : "Disconnected";

  return (
    <div className="flex items-center gap-4 text-xs">
      <div className="flex items-center gap-2">
        <span className={`status-dot ${state}`} />
        <span className="text-terminal-muted">{stateLabel}</span>
      </div>

      {lastPingMs !== null && (
        <span className="text-terminal-muted">
          {lastPingMs}ms
        </span>
      )}

      {backendStatus && (
        <span className="text-terminal-muted">
          Clients: {backendStatus.connected_clients}
        </span>
      )}
    </div>
  );
}
