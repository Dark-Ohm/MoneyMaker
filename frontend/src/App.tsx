import { useState } from "react";
import { useBackendSocket } from "./hooks/useBackendSocket";
import { TickerStream } from "./components/TickerStream";
import { ConnectionStatus } from "./components/ConnectionStatus";
import { LogPanel } from "./components/LogPanel";
import { ChartPanel } from "./components/ChartPanel";

export default function App() {
  const { ticks, logs, status, connectionState, lastPingMs } = useBackendSocket();
  const [activeSymbol, setActiveSymbol] = useState("BTC/USDT");
  const backendPort = status?.port ?? 0;
  const baseUrl = `http://127.0.0.1:${backendPort}`;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-terminal-border px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-bold tracking-tight">
            <span className="text-terminal-accent">$</span> MoneyMaker
          </h1>
          <span className="text-xs text-terminal-muted">v0.1.0</span>
        </div>
        <ConnectionStatus
          state={connectionState}
          lastPingMs={lastPingMs}
          backendStatus={status}
        />
      </header>

      <main className="flex-1 p-4 grid grid-cols-12 gap-4 overflow-hidden">
        {/* Left: Tickers + Chart */}
        <div className="col-span-9 flex flex-col gap-4 overflow-hidden">
          <div className="shrink-0">
            <TickerStream
              ticks={ticks}
              onSelectSymbol={setActiveSymbol}
              activeSymbol={activeSymbol}
            />
          </div>
          <div className="flex-1 min-h-0">
            {backendPort > 0 && (
              <ChartPanel baseUrl={baseUrl} activeSymbol={activeSymbol} />
            )}
          </div>
        </div>

        {/* Right: Log Panel */}
        <div className="col-span-3 flex flex-col overflow-hidden">
          <h2 className="text-sm font-semibold text-terminal-muted uppercase tracking-wider mb-2 shrink-0">
            Activity Log
          </h2>
          <div className="flex-1 min-h-0">
            <LogPanel logs={logs} />
          </div>
        </div>
      </main>

      <footer className="border-t border-terminal-border px-6 py-2 text-xs text-terminal-muted flex justify-between">
        <span>WebSocket: {connectionState}</span>
        <span>Backend port: {status?.port ?? "—"}</span>
        <span>Latency: {lastPingMs !== null ? `${lastPingMs}ms` : "—"}</span>
      </footer>
    </div>
  );
}
