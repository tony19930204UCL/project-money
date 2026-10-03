// src/lib/utils/kbars.ts — KBars column arrays -> candles, aggregation

import type { Candle, KBars } from '../types/market';

// Preserve this helper for close-label-right aggregation tests and callers that
// intentionally work with a wall-clock encoded as UTC.
export function wallClockToUtc(dt: string): number {
    const y = Number(dt.slice(0, 4));
    const mo = Number(dt.slice(5, 7));
    const d = Number(dt.slice(8, 10));
    const h = Number(dt.slice(11, 13)) || 0;
    const mi = Number(dt.slice(14, 16)) || 0;
    const s = Number(dt.slice(17, 19)) || 0;
    return Date.UTC(y, mo - 1, d, h, mi, s) / 1000;
}

const EXPLICIT_TIME_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/** Convert API/Shioaji timestamps to a real Unix epoch.
 *
 * Compatibility API timestamps include a UTC offset. Native Shioaji can
 * return timezone-less Taiwan wall-clock strings, interpreted as +08:00.
 */
export function marketDateTimeToUnix(dt: string): number {
    if (EXPLICIT_TIME_ZONE.test(dt)) {
        return Math.floor(Date.parse(dt) / 1000);
    }
    return wallClockToUtc(dt) - 8 * 60 * 60;
}

export function kbarsToCandles(k: KBars): Candle[] {
    const out: Candle[] = [];
    for (let i = 0; i < k.datetime.length; i++) {
        const dt = k.datetime[i];
        if (!dt) continue;
        out.push({
            time: marketDateTimeToUnix(dt),
            open: k.Open[i] ?? 0,
            high: k.High[i] ?? 0,
            low: k.Low[i] ?? 0,
            close: k.Close[i] ?? 0,
            volume: k.Volume[i] ?? 0,
        });
    }
    out.sort((a, b) => a.time - b.time);
    return out;
}

// Aggregate 1-minute candles into N-minute or daily bars.
// 1 分 K 是 close-label-right（label 08:46 = 08:45:00–08:45:59 成交），
// N 分 K 必須沿用同一慣例：ceil 到桶的收盤 label（5 分 K = 08:50、
// 08:55…13:45，08:50 那根 = label 08:46–08:50）。floor 會整體早移
// 一分鐘且開盤桶只剩 4 根。日 K 維持日曆日。
export function aggregate(candles: Candle[], minutes: number): Candle[] {
    if (minutes <= 1) return candles;
    const out: Candle[] = [];
    let cur: Candle | null = null;
    const bucketSec = minutes * 60;
    for (const c of candles) {
        const bucket =
            minutes >= 1440
                ? Math.floor(c.time / 86400) * 86400
                : Math.ceil(c.time / bucketSec) * bucketSec;
        if (!cur || cur.time !== bucket) {
            if (cur) out.push(cur);
            cur = { ...c, time: bucket };
        } else {
            cur.high = Math.max(cur.high, c.high);
            cur.low = Math.min(cur.low, c.low);
            cur.close = c.close;
            cur.volume += c.volume;
        }
    }
    if (cur) out.push(cur);
    return out;
}

// Real Unix time keeps chart updates aligned with offset-aware history rows.
export function nowWallClockUtc(): number {
    return Math.floor(Date.now() / 1000);
}

export function dateStrOffset(daysAgo: number): string {
    const d = new Date(Date.now() - daysAgo * 86400_000);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}
