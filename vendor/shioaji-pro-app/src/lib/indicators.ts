// src/lib/indicators.ts — pure indicator computations on candles.
// Rendering/registry lives in indicator-defs.ts; this file is math only.
// Points may carry value: undefined to encode a gap (whitespace data).

import type { Candle } from './types/market';

export interface IndicatorPoint {
    time: number;
    value?: number;
}

const tp = (b: Candle) => (b.high + b.low + b.close) / 3;

export function sma(bars: Candle[], period: number): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    let sum = 0;
    for (let i = 0; i < bars.length; i++) {
        sum += bars[i]!.close;
        if (i >= period) sum -= bars[i - period]!.close;
        if (i >= period - 1) {
            out.push({ time: bars[i]!.time, value: sum / period });
        }
    }
    return out;
}

// EMA seeded with the SMA of the first `period` closes (standard seeding)
export function ema(bars: Candle[], period: number): IndicatorPoint[] {
    return emaOf(
        bars.map((b) => ({ time: b.time, value: b.close })),
        period,
    );
}

function emaOf(points: IndicatorPoint[], period: number): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    const k = 2 / (period + 1);
    let prev: number | null = null;
    let seedSum = 0;
    let seedCount = 0;
    for (const p of points) {
        if (p.value === undefined) continue;
        if (prev === null) {
            seedSum += p.value;
            seedCount += 1;
            if (seedCount === period) {
                prev = seedSum / period;
                out.push({ time: p.time, value: prev });
            }
            continue;
        }
        prev = p.value * k + prev * (1 - k);
        out.push({ time: p.time, value: prev });
    }
    return out;
}

export function wma(bars: Candle[], period: number): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    const denom = (period * (period + 1)) / 2;
    for (let i = period - 1; i < bars.length; i++) {
        let sum = 0;
        for (let j = 0; j < period; j++) {
            sum += bars[i - j]!.close * (period - j);
        }
        out.push({ time: bars[i]!.time, value: sum / denom });
    }
    return out;
}

export function bollinger(
    bars: Candle[],
    period = 20,
    mult = 2,
): { mid: IndicatorPoint[]; upper: IndicatorPoint[]; lower: IndicatorPoint[] } {
    const mid: IndicatorPoint[] = [];
    const upper: IndicatorPoint[] = [];
    const lower: IndicatorPoint[] = [];
    let sum = 0;
    let sqSum = 0;
    for (let i = 0; i < bars.length; i++) {
        const c = bars[i]!.close;
        sum += c;
        sqSum += c * c;
        if (i >= period) {
            const o = bars[i - period]!.close;
            sum -= o;
            sqSum -= o * o;
        }
        if (i >= period - 1) {
            const mean = sum / period;
            const sd = Math.sqrt(Math.max(0, sqSum / period - mean * mean));
            const t = bars[i]!.time;
            mid.push({ time: t, value: mean });
            upper.push({ time: t, value: mean + mult * sd });
            lower.push({ time: t, value: mean - mult * sd });
        }
    }
    return { mid, upper, lower };
}

// VWAP resets at each trading day boundary
export function vwap(bars: Candle[]): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    let pv = 0;
    let vol = 0;
    let day = -1;
    for (const b of bars) {
        const d = Math.floor(b.time / 86400);
        if (d !== day) {
            day = d;
            pv = 0;
            vol = 0;
        }
        pv += tp(b) * b.volume;
        vol += b.volume;
        if (vol > 0) out.push({ time: b.time, value: pv / vol });
    }
    return out;
}

// Parabolic SAR (Wilder)
export function sar(bars: Candle[], step = 0.02, max = 0.2): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    if (bars.length < 2) return out;
    let rising = bars[1]!.close >= bars[0]!.close;
    let cur = rising ? bars[0]!.low : bars[0]!.high;
    let ep = rising ? bars[0]!.high : bars[0]!.low;
    let af = step;
    for (let i = 1; i < bars.length; i++) {
        const b = bars[i]!;
        cur = cur + af * (ep - cur);
        if (rising) {
            cur = Math.min(cur, bars[i - 1]!.low, bars[i - 2]?.low ?? Infinity);
            if (b.low < cur) {
                rising = false;
                cur = ep;
                ep = b.low;
                af = step;
            } else if (b.high > ep) {
                ep = b.high;
                af = Math.min(max, af + step);
            }
        } else {
            cur = Math.max(
                cur,
                bars[i - 1]!.high,
                bars[i - 2]?.high ?? -Infinity,
            );
            if (b.high > cur) {
                rising = true;
                cur = ep;
                ep = b.high;
                af = step;
            } else if (b.low < ep) {
                ep = b.low;
                af = Math.min(max, af + step);
            }
        }
        out.push({ time: b.time, value: cur });
    }
    return out;
}

export function donchian(
    bars: Candle[],
    period = 20,
): { upper: IndicatorPoint[]; mid: IndicatorPoint[]; lower: IndicatorPoint[] } {
    const upper: IndicatorPoint[] = [];
    const mid: IndicatorPoint[] = [];
    const lower: IndicatorPoint[] = [];
    for (let i = period - 1; i < bars.length; i++) {
        let hi = -Infinity;
        let lo = Infinity;
        for (let j = i - period + 1; j <= i; j++) {
            hi = Math.max(hi, bars[j]!.high);
            lo = Math.min(lo, bars[j]!.low);
        }
        const t = bars[i]!.time;
        upper.push({ time: t, value: hi });
        lower.push({ time: t, value: lo });
        mid.push({ time: t, value: (hi + lo) / 2 });
    }
    return { upper, mid, lower };
}

// true range series (index-aligned with bars, first bar = high-low)
function trueRanges(bars: Candle[]): number[] {
    const tr: number[] = [];
    for (let i = 0; i < bars.length; i++) {
        const b = bars[i]!;
        if (i === 0) {
            tr.push(b.high - b.low);
            continue;
        }
        const pc = bars[i - 1]!.close;
        tr.push(
            Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc)),
        );
    }
    return tr;
}

// Wilder smoothing (RMA)
function rma(values: number[], period: number): (number | undefined)[] {
    const out: (number | undefined)[] = [];
    let prev: number | null = null;
    let seed = 0;
    for (let i = 0; i < values.length; i++) {
        if (prev === null) {
            seed += values[i]!;
            if (i === period - 1) {
                prev = seed / period;
                out.push(prev);
            } else {
                out.push(undefined);
            }
            continue;
        }
        prev = (prev * (period - 1) + values[i]!) / period;
        out.push(prev);
    }
    return out;
}

export function atr(bars: Candle[], period = 14): IndicatorPoint[] {
    const smoothed = rma(trueRanges(bars), period);
    const out: IndicatorPoint[] = [];
    for (let i = 0; i < bars.length; i++) {
        const v = smoothed[i];
        if (v !== undefined) out.push({ time: bars[i]!.time, value: v });
    }
    return out;
}

export function keltner(
    bars: Candle[],
    emaPeriod = 20,
    atrPeriod = 10,
    mult = 2,
): { mid: IndicatorPoint[]; upper: IndicatorPoint[]; lower: IndicatorPoint[] } {
    const midLine = ema(bars, emaPeriod);
    const atrLine = atr(bars, atrPeriod);
    const atrAt = new Map(atrLine.map((p) => [p.time, p.value!]));
    const mid: IndicatorPoint[] = [];
    const upper: IndicatorPoint[] = [];
    const lower: IndicatorPoint[] = [];
    for (const p of midLine) {
        const a = atrAt.get(p.time);
        if (a === undefined || p.value === undefined) continue;
        mid.push(p);
        upper.push({ time: p.time, value: p.value + mult * a });
        lower.push({ time: p.time, value: p.value - mult * a });
    }
    return { mid, upper, lower };
}

// SuperTrend — two series (up-trend line below price / down-trend line above)
// with whitespace gaps so the inactive side isn't drawn
export function supertrend(
    bars: Candle[],
    period = 10,
    mult = 3,
): { up: IndicatorPoint[]; down: IndicatorPoint[] } {
    const atrLine = rma(trueRanges(bars), period);
    const up: IndicatorPoint[] = [];
    const down: IndicatorPoint[] = [];
    let prevUpper = NaN;
    let prevLower = NaN;
    let trendUp = true;
    let prevClose = NaN;
    for (let i = 0; i < bars.length; i++) {
        const b = bars[i]!;
        const a = atrLine[i];
        if (a === undefined) {
            up.push({ time: b.time });
            down.push({ time: b.time });
            prevClose = b.close;
            continue;
        }
        const mid = (b.high + b.low) / 2;
        let upper = mid + mult * a;
        let lower = mid - mult * a;
        // band ratchet
        if (!Number.isNaN(prevUpper) && (upper > prevUpper || prevClose > prevUpper)) {
            upper = Math.min(upper, prevUpper);
        }
        if (!Number.isNaN(prevLower) && (lower < prevLower || prevClose < prevLower)) {
            lower = Math.max(lower, prevLower);
        }
        if (trendUp && b.close < lower) trendUp = false;
        else if (!trendUp && b.close > upper) trendUp = true;
        up.push(trendUp ? { time: b.time, value: lower } : { time: b.time });
        down.push(trendUp ? { time: b.time } : { time: b.time, value: upper });
        prevUpper = upper;
        prevLower = lower;
        prevClose = b.close;
    }
    return { up, down };
}

// ---- oscillators（副圖）----

export function rsi(bars: Candle[], period = 14): IndicatorPoint[] {
    const gains: number[] = [];
    const losses: number[] = [];
    for (let i = 1; i < bars.length; i++) {
        const chg = bars[i]!.close - bars[i - 1]!.close;
        gains.push(Math.max(0, chg));
        losses.push(Math.max(0, -chg));
    }
    const avgG = rma(gains, period);
    const avgL = rma(losses, period);
    const out: IndicatorPoint[] = [];
    for (let i = 0; i < gains.length; i++) {
        const g = avgG[i];
        const l = avgL[i];
        if (g === undefined || l === undefined) continue;
        const v = l === 0 ? 100 : 100 - 100 / (1 + g / l);
        out.push({ time: bars[i + 1]!.time, value: v });
    }
    return out;
}

export function macd(
    bars: Candle[],
    fast = 12,
    slow = 26,
    signalPeriod = 9,
): { macd: IndicatorPoint[]; signal: IndicatorPoint[]; hist: IndicatorPoint[] } {
    const fastE = ema(bars, fast);
    const slowE = ema(bars, slow);
    const fastAt = new Map(fastE.map((p) => [p.time, p.value!]));
    const macdLine: IndicatorPoint[] = [];
    for (const p of slowE) {
        const f = fastAt.get(p.time);
        if (f === undefined || p.value === undefined) continue;
        macdLine.push({ time: p.time, value: f - p.value });
    }
    const signal = emaOf(macdLine, signalPeriod);
    const sigAt = new Map(signal.map((p) => [p.time, p.value!]));
    const hist: IndicatorPoint[] = [];
    for (const p of macdLine) {
        const s = sigAt.get(p.time);
        if (s === undefined || p.value === undefined) continue;
        hist.push({ time: p.time, value: p.value - s });
    }
    return { macd: macdLine, signal, hist };
}

// KD（Stochastic）台股慣用 (9,3,3)：RSV 的 SMA 平滑
export function stoch(
    bars: Candle[],
    kPeriod = 9,
    kSmooth = 3,
    dPeriod = 3,
): { k: IndicatorPoint[]; d: IndicatorPoint[] } {
    const rsv: IndicatorPoint[] = [];
    for (let i = kPeriod - 1; i < bars.length; i++) {
        let hi = -Infinity;
        let lo = Infinity;
        for (let j = i - kPeriod + 1; j <= i; j++) {
            hi = Math.max(hi, bars[j]!.high);
            lo = Math.min(lo, bars[j]!.low);
        }
        const range = hi - lo;
        rsv.push({
            time: bars[i]!.time,
            value: range === 0 ? 50 : ((bars[i]!.close - lo) / range) * 100,
        });
    }
    const k = smaOf(rsv, kSmooth);
    const d = smaOf(k, dPeriod);
    return { k, d };
}

function smaOf(points: IndicatorPoint[], period: number): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    let sum = 0;
    const vals: number[] = [];
    for (const p of points) {
        if (p.value === undefined) continue;
        vals.push(p.value);
        sum += p.value;
        if (vals.length > period) sum -= vals[vals.length - period - 1]!;
        if (vals.length >= period) {
            out.push({ time: p.time, value: sum / period });
        }
    }
    return out;
}

export function stochRsi(
    bars: Candle[],
    rsiPeriod = 14,
    stochPeriod = 14,
    kSmooth = 3,
    dSmooth = 3,
): { k: IndicatorPoint[]; d: IndicatorPoint[] } {
    const r = rsi(bars, rsiPeriod);
    const raw: IndicatorPoint[] = [];
    for (let i = stochPeriod - 1; i < r.length; i++) {
        let hi = -Infinity;
        let lo = Infinity;
        for (let j = i - stochPeriod + 1; j <= i; j++) {
            hi = Math.max(hi, r[j]!.value!);
            lo = Math.min(lo, r[j]!.value!);
        }
        const range = hi - lo;
        raw.push({
            time: r[i]!.time,
            value: range === 0 ? 50 : ((r[i]!.value! - lo) / range) * 100,
        });
    }
    const k = smaOf(raw, kSmooth);
    const d = smaOf(k, dSmooth);
    return { k, d };
}

export function cci(bars: Candle[], period = 20): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    for (let i = period - 1; i < bars.length; i++) {
        let sum = 0;
        for (let j = i - period + 1; j <= i; j++) sum += tp(bars[j]!);
        const mean = sum / period;
        let dev = 0;
        for (let j = i - period + 1; j <= i; j++) {
            dev += Math.abs(tp(bars[j]!) - mean);
        }
        const md = dev / period;
        out.push({
            time: bars[i]!.time,
            value: md === 0 ? 0 : (tp(bars[i]!) - mean) / (0.015 * md),
        });
    }
    return out;
}

export function obv(bars: Candle[]): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    let acc = 0;
    for (let i = 0; i < bars.length; i++) {
        if (i > 0) {
            const chg = bars[i]!.close - bars[i - 1]!.close;
            if (chg > 0) acc += bars[i]!.volume;
            else if (chg < 0) acc -= bars[i]!.volume;
        }
        out.push({ time: bars[i]!.time, value: acc });
    }
    return out;
}

export function mfi(bars: Candle[], period = 14): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    const pos: number[] = [];
    const neg: number[] = [];
    for (let i = 1; i < bars.length; i++) {
        const cur = tp(bars[i]!);
        const prev = tp(bars[i - 1]!);
        const flow = cur * bars[i]!.volume;
        pos.push(cur > prev ? flow : 0);
        neg.push(cur < prev ? flow : 0);
        if (pos.length > period) {
            pos.shift();
            neg.shift();
        }
        if (pos.length === period) {
            const p = pos.reduce((a, b) => a + b, 0);
            const n = neg.reduce((a, b) => a + b, 0);
            out.push({
                time: bars[i]!.time,
                value: n === 0 ? 100 : 100 - 100 / (1 + p / n),
            });
        }
    }
    return out;
}

export function willr(bars: Candle[], period = 14): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    for (let i = period - 1; i < bars.length; i++) {
        let hi = -Infinity;
        let lo = Infinity;
        for (let j = i - period + 1; j <= i; j++) {
            hi = Math.max(hi, bars[j]!.high);
            lo = Math.min(lo, bars[j]!.low);
        }
        const range = hi - lo;
        out.push({
            time: bars[i]!.time,
            value: range === 0 ? -50 : ((hi - bars[i]!.close) / range) * -100,
        });
    }
    return out;
}

export function dmi(
    bars: Candle[],
    period = 14,
    adxPeriod = 14,
): { plus: IndicatorPoint[]; minus: IndicatorPoint[]; adx: IndicatorPoint[] } {
    const plusDM: number[] = [];
    const minusDM: number[] = [];
    const tr: number[] = [];
    for (let i = 1; i < bars.length; i++) {
        const upMove = bars[i]!.high - bars[i - 1]!.high;
        const downMove = bars[i - 1]!.low - bars[i]!.low;
        plusDM.push(upMove > downMove && upMove > 0 ? upMove : 0);
        minusDM.push(downMove > upMove && downMove > 0 ? downMove : 0);
        const pc = bars[i - 1]!.close;
        tr.push(
            Math.max(
                bars[i]!.high - bars[i]!.low,
                Math.abs(bars[i]!.high - pc),
                Math.abs(bars[i]!.low - pc),
            ),
        );
    }
    const sTR = rma(tr, period);
    const sPlus = rma(plusDM, period);
    const sMinus = rma(minusDM, period);
    const plus: IndicatorPoint[] = [];
    const minus: IndicatorPoint[] = [];
    const dx: IndicatorPoint[] = [];
    for (let i = 0; i < tr.length; i++) {
        const t = sTR[i];
        const p = sPlus[i];
        const m = sMinus[i];
        if (t === undefined || p === undefined || m === undefined || t === 0) {
            continue;
        }
        const time = bars[i + 1]!.time;
        const pdi = (p / t) * 100;
        const mdi = (m / t) * 100;
        plus.push({ time, value: pdi });
        minus.push({ time, value: mdi });
        const sum = pdi + mdi;
        dx.push({ time, value: sum === 0 ? 0 : (Math.abs(pdi - mdi) / sum) * 100 });
    }
    // ADX = RMA of DX
    const adxVals = rma(
        dx.map((p) => p.value!),
        adxPeriod,
    );
    const adx: IndicatorPoint[] = [];
    for (let i = 0; i < dx.length; i++) {
        const v = adxVals[i];
        if (v !== undefined) adx.push({ time: dx[i]!.time, value: v });
    }
    return { plus, minus, adx };
}

export function roc(bars: Candle[], period = 12): IndicatorPoint[] {
    const out: IndicatorPoint[] = [];
    for (let i = period; i < bars.length; i++) {
        const base = bars[i - period]!.close;
        if (base === 0) continue;
        out.push({
            time: bars[i]!.time,
            value: ((bars[i]!.close - base) / base) * 100,
        });
    }
    return out;
}

// 乖離率 BIAS = (close - MA) / MA × 100
export function bias(bars: Candle[], period = 20): IndicatorPoint[] {
    const ma = sma(bars, period);
    const out: IndicatorPoint[] = [];
    const maAt = new Map(ma.map((p) => [p.time, p.value!]));
    for (const b of bars) {
        const m = maAt.get(b.time);
        if (m === undefined || m === 0) continue;
        out.push({ time: b.time, value: ((b.close - m) / m) * 100 });
    }
    return out;
}
