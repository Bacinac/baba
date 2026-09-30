import { formatNumber } from "$lib/kit";

/** Adaptive-precision gauge value: 2 dp below 10, 1 dp below 100, else 0. */
export function formatMetric(n: number): string {
  if (!isFinite(n)) return "—";
  const a = Math.abs(n);
  const dp = a >= 100 ? 0 : a >= 10 ? 1 : 2;
  return formatNumber(n, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Seconds → short duration ("45s", "1m 5s"). */
export function formatDuration(s: number): string {
  if (s < 60) return `${formatNumber(Math.floor(s))}s`;
  const m = Math.floor(s / 60);
  return `${formatNumber(m)}m ${Math.floor(s % 60)}s`;
}

/** Seconds → coarse uptime ("3d 4h" / "5h 6m" / "7m"). */
export function formatUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
