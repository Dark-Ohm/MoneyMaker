import { useEffect, useRef, useState, useCallback } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type Time,
  type SeriesMarker,
  ColorType,
  CrosshairMode,
} from "lightweight-charts";

interface TickRow {
  timestamp: string;
  market: string;
  symbol: string;
  price: number;
  volume_24h: number;
  change_pct: number;
}

interface SignalRow {
  timestamp: string;
  agent_id: string;
  symbol: string;
  action: string;
  reasoning: string;
}

interface ChartPanelProps {
  baseUrl: string;
  activeSymbol: string;
}

export function ChartPanel({ baseUrl, activeSymbol }: ChartPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [symbol, setSymbol] = useState(activeSymbol);
  const [symbols, setSymbols] = useState<string[]>([]);

  const loadChart = useCallback(async () => {
    if (!containerRef.current) return;

    // Clean up previous chart
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
      seriesRef.current = null;
    }

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#111827" },
        textColor: "#94a3b8",
        fontFamily: "'JetBrains Mono', monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#3b82f680", width: 1, style: 2 },
        horzLine: { color: "#3b82f680", width: 1, style: 2 },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: true,
        borderColor: "#1e293b",
      },
      rightPriceScale: {
        borderColor: "#1e293b",
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });

    const series = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderDownColor: "#ef4444",
      borderUpColor: "#22c55e",
      wickDownColor: "#ef444480",
      wickUpColor: "#22c55e80",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // Load historical data
    try {
      const [tickRes, signalRes] = await Promise.all([
        fetch(`${baseUrl}/api/chart/ticks?symbol=${symbol}&limit=500`),
        fetch(`${baseUrl}/api/chart/signals?symbol=${symbol}&limit=100`),
      ]);

      const tickData = await tickRes.json();
      const signalData = await signalRes.json();

      // Convert ticks to candlestick data (group into 5s candles)
      const candles = buildCandles(tickData.ticks || []);
      if (candles.length > 0) {
        series.setData(candles);
        chart.timeScale().fitContent();
      }

      // Add signal markers
      const markers = buildMarkers(signalData.signals || []);
      if (markers.length > 0) {
        series.setMarkers(markers);
      }

      // Discover available symbols
      const symRes = await fetch(`${baseUrl}/api/chart/ticks?limit=1000`);
      const symData = await symRes.json();
      const uniqueSymbols = [...new Set((symData.ticks || []).map((t: TickRow) => t.symbol))] as string[];
      if (uniqueSymbols.length > 0) setSymbols(uniqueSymbols);
    } catch (e) {
      console.error("Failed to load chart data:", e);
    }

    // Resize handler
    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
    };
  }, [baseUrl, symbol]);

  useEffect(() => {
    const cleanup = loadChart();
    return () => { cleanup.then(fn => fn?.()); };
  }, [loadChart]);

  // Subscribe to real-time ticks via parent's tick state
  useEffect(() => {
    if (!seriesRef.current) return;

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${baseUrl}/api/chart/ticks?symbol=${symbol}&limit=5`);
        const data = await res.json();
        const ticks: TickRow[] = data.ticks || [];
        if (ticks.length > 0) {
          const latest = ticks[ticks.length - 1];
          const time = Math.floor(new Date(latest.timestamp).getTime() / 1000) as Time;
          seriesRef.current?.update({
            time,
            open: latest.price * 0.999,
            high: latest.price * 1.001,
            low: latest.price * 0.998,
            close: latest.price,
          });
        }
      } catch {
        // ignore
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [baseUrl, symbol]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-sm font-semibold text-terminal-muted uppercase tracking-wider">
          Price Chart
        </h2>
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bg-terminal-surface border border-terminal-border rounded px-2 py-1 text-xs text-gray-300 focus:outline-none focus:border-terminal-accent"
        >
          {symbols.length === 0 && <option value={symbol}>{symbol}</option>}
          {symbols.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>
      <div
        ref={containerRef}
        className="flex-1 bg-terminal-surface border border-terminal-border rounded-lg overflow-hidden"
        style={{ minHeight: 300 }}
      />
    </div>
  );
}

function buildCandles(ticks: TickRow[]): CandlestickData<Time>[] {
  const buckets = new Map<number, { open: number; high: number; low: number; close: number }>();

  for (const tick of ticks) {
    const ts = Math.floor(new Date(tick.timestamp).getTime() / 1000);
    const bucket = Math.floor(ts / 5) * 5; // 5-second candles

    const existing = buckets.get(bucket);
    if (existing) {
      existing.high = Math.max(existing.high, tick.price);
      existing.low = Math.min(existing.low, tick.price);
      existing.close = tick.price;
    } else {
      buckets.set(bucket, {
        open: tick.price,
        high: tick.price,
        low: tick.price,
        close: tick.price,
      });
    }
  }

  return Array.from(buckets.entries())
    .sort(([a], [b]) => a - b)
    .map(([time, data]) => ({
      time: time as Time,
      ...data,
    }));
}

function buildMarkers(signals: SignalRow[]): SeriesMarker<Time>[] {
  return signals
    .filter((s) => s.action && s.action !== "hold")
    .map((s) => ({
      time: Math.floor(new Date(s.timestamp).getTime() / 1000) as Time,
      position: (s.action === "buy" || s.action === "YES" ? "belowBar" : "aboveBar") as "belowBar" | "aboveBar",
      color: s.action === "buy" || s.action === "YES" ? "#22c55e" : "#ef4444",
      shape: (s.action === "buy" || s.action === "YES" ? "arrowUp" : "arrowDown") as "arrowUp" | "arrowDown",
      text: `${s.action} ${s.agent_id}`,
    }));
}
