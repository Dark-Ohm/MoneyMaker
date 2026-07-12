import type { TickData } from "../hooks/useBackendSocket";

interface TickerStreamProps {
  ticks: TickData[];
  onSelectSymbol?: (symbol: string) => void;
  activeSymbol?: string;
}

function formatPrice(tick: TickData): string {
  if (tick.market === "polymarket") {
    return `${(tick.price * 100).toFixed(1)}%`;
  }
  if (tick.price >= 1000) {
    return `$${tick.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (tick.price >= 1) {
    return `$${tick.price.toFixed(4)}`;
  }
  return `$${tick.price.toFixed(6)}`;
}

function formatVolume(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toFixed(0);
}

function timeAgo(ts: string): string {
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 1) return "just now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  return `${Math.floor(diff / 60)}m ago`;
}

export function TickerStream({ ticks, onSelectSymbol, activeSymbol }: TickerStreamProps) {
  const latestBySymbol = new Map<string, TickData>();
  for (const tick of ticks) {
    const existing = latestBySymbol.get(tick.symbol);
    if (!existing || new Date(tick.timestamp) > new Date(existing.timestamp)) {
      latestBySymbol.set(tick.symbol, tick);
    }
  }

  const sorted = Array.from(latestBySymbol.values()).sort((a, b) => {
    if (a.market === "polymarket" && b.market !== "polymarket") return 1;
    if (a.market !== "polymarket" && b.market === "polymarket") return -1;
    return b.volume_24h - a.volume_24h;
  });

  if (sorted.length === 0) {
    return (
      <div className="ticker-card text-terminal-muted text-sm flex items-center justify-center h-16">
        Waiting for ticks...
      </div>
    );
  }

  return (
    <div className="flex gap-2 overflow-x-auto pb-1">
      {sorted.map((tick) => {
        const isUp = tick.change_pct >= 0;
        const isActive = tick.symbol === activeSymbol;
        const color = tick.market === "polymarket"
          ? "text-terminal-yellow"
          : isUp
            ? "text-terminal-green"
            : "text-terminal-red";

        return (
          <button
            key={tick.symbol}
            onClick={() => onSelectSymbol?.(tick.symbol)}
            className={`ticker-card group shrink-0 min-w-[180px] text-left cursor-pointer transition-all ${
              isActive ? "border-terminal-accent ring-1 ring-terminal-accent/30" : ""
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <div>
                <span className="font-semibold text-sm">{tick.symbol}</span>
                <span className="ml-2 text-[10px] uppercase text-terminal-muted bg-terminal-border px-1.5 py-0.5 rounded">
                  {tick.market}
                </span>
              </div>
              <span className="text-[10px] text-terminal-muted">{timeAgo(tick.timestamp)}</span>
            </div>

            <div className={`text-xl font-bold ${color} mb-1`}>
              {formatPrice(tick)}
            </div>

            <div className="flex items-center gap-3 text-xs text-terminal-muted">
              <span className={color}>
                {isUp ? "▲" : "▼"} {Math.abs(tick.change_pct).toFixed(2)}%
              </span>
              <span>Vol: {formatVolume(tick.volume_24h)}</span>
              {tick.market === "polymarket" && (tick.extra.liquidity_usd as number) > 0 && (
                <span>Liq: ${formatVolume(tick.extra.liquidity_usd as number)}</span>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
