import type { LogEntry } from "../hooks/useBackendSocket";

interface LogPanelProps {
  logs: LogEntry[];
}

function formatLogTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString("en-US", { hour12: false });
  } catch {
    return "--:--:--";
  }
}

function typeColor(type: string): string {
  switch (type) {
    case "tick": return "text-terminal-accent";
    case "signal": return "text-terminal-yellow";
    case "status": return "text-terminal-green";
    case "error": return "text-terminal-red";
    default: return "text-terminal-muted";
  }
}

function formatEntry(log: LogEntry): string {
  const d = log.data;
  const parts: string[] = [];
  if (d.symbol) parts.push(String(d.symbol));
  if (d.price !== undefined) parts.push(String(d.price));
  if (d.agent_id) parts.push(String(d.agent_id));
  if (d.running !== undefined) parts.push(d.running ? "● RUNNING" : "○ STOPPED");
  if (d.message) parts.push(String(d.message));
  if (d.status) parts.push(String(d.status));
  if (d.reason) parts.push(String(d.reason));
  return parts.join(" ") || JSON.stringify(d);
}

export function LogPanel({ logs }: LogPanelProps) {
  return (
    <div className="bg-terminal-surface border border-terminal-border rounded-lg p-3 h-[calc(100vh-180px)] overflow-y-auto font-mono text-xs">
      {logs.length === 0 ? (
        <div className="text-terminal-muted text-center py-8">
          No activity yet
        </div>
      ) : (
        <div className="space-y-1">
          {logs.map((log, i) => (
            <div key={i} className="flex gap-2 hover:bg-white/5 rounded px-1 py-0.5">
              <span className="text-terminal-muted shrink-0 w-[70px]">
                {formatLogTime(log.timestamp)}
              </span>
              <span className={`shrink-0 w-[50px] uppercase font-semibold ${typeColor(log.type)}`}>
                {log.type}
              </span>
              <span className="text-gray-300 break-all">
                {formatEntry(log)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
