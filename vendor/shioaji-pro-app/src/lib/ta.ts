// src/lib/ta.ts — array-based TA helpers exposed to user-defined (custom)
// indicators as the `ta.*` namespace. All functions take and return series
// aligned to the bar index; null marks warm-up gaps or missing data and
// propagates through calculations. Pure module — also runs inside the
// validation Web Worker, so no DOM / app imports here.

export type Ser = (number | null)[];
export type SerIn = ArrayLike<number | null | undefined>;

const num = (v: number | null | undefined): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? v : null;

// normalize arbitrary user input into a clean Ser
export function toSer(src: SerIn): Ser {
    const out: Ser = new Array(src.length);
    for (let i = 0; i < src.length; i++) out[i] = num(src[i]);
    return out;
}

function rolling(
    srcIn: SerIn,
    n: number,
    f: (win: number[]) => number,
): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    if (!Number.isFinite(n) || n < 1) return out;
    n = Math.floor(n);
    for (let i = n - 1; i < src.length; i++) {
        const win: number[] = [];
        let ok = true;
        for (let j = i - n + 1; j <= i; j++) {
            const v = src[j];
            if (v === null) {
                ok = false;
                break;
            }
            win.push(v as number);
        }
        if (ok) out[i] = f(win);
    }
    return out;
}

// recursive smoother: seed with the mean of the first full window, then
// out = prev + alpha * (v - prev); null input keeps the previous state
// but emits null for that bar
function smooth(srcIn: SerIn, n: number, alpha: number): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    if (!Number.isFinite(n) || n < 1) return out;
    n = Math.floor(n);
    let seedSum = 0;
    let seedCount = 0;
    let prev: number | null = null;
    for (let i = 0; i < src.length; i++) {
        const v = src[i] ?? null;
        if (v === null) {
            if (prev === null) {
                seedSum = 0;
                seedCount = 0; // broken warm-up window — restart seeding
            }
            continue;
        }
        if (prev === null) {
            seedSum += v;
            seedCount++;
            if (seedCount === n) prev = seedSum / n;
        } else {
            prev = prev + alpha * (v - prev);
        }
        if (prev !== null) out[i] = prev;
    }
    return out;
}

export const sma = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => w.reduce((a, b) => a + b, 0) / w.length);

export const ema = (src: SerIn, n: number): Ser =>
    smooth(src, n, 2 / (Math.floor(n) + 1));

// Wilder smoothing (RMA) — RSI/ATR/DMI 系列用的平滑
export const rma = (src: SerIn, n: number): Ser =>
    smooth(src, n, 1 / Math.floor(n));

export const wma = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => {
        let s = 0;
        let d = 0;
        for (let i = 0; i < w.length; i++) {
            s += w[i]! * (i + 1);
            d += i + 1;
        }
        return s / d;
    });

export const stdev = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => {
        const m = w.reduce((a, b) => a + b, 0) / w.length;
        return Math.sqrt(
            w.reduce((a, b) => a + (b - m) * (b - m), 0) / w.length,
        );
    });

export const highest = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => Math.max(...w));

export const lowest = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => Math.min(...w));

export const sum = (src: SerIn, n: number): Ser =>
    rolling(src, n, (w) => w.reduce((a, b) => a + b, 0));

// v[i] - v[i-n]
export function change(srcIn: SerIn, n = 1): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    n = Math.max(1, Math.floor(n));
    for (let i = n; i < src.length; i++) {
        const a = src[i] ?? null;
        const b = src[i - n] ?? null;
        if (a !== null && b !== null) out[i] = a - b;
    }
    return out;
}

// N 期變動百分比
export function roc(srcIn: SerIn, n: number): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    n = Math.max(1, Math.floor(n));
    for (let i = n; i < src.length; i++) {
        const a = src[i] ?? null;
        const b = src[i - n] ?? null;
        if (a !== null && b !== null && b !== 0) {
            out[i] = ((a - b) / Math.abs(b)) * 100;
        }
    }
    return out;
}

export function rsi(srcIn: SerIn, n: number): Ser {
    const src = toSer(srcIn);
    const gains: Ser = new Array(src.length).fill(null);
    const losses: Ser = new Array(src.length).fill(null);
    for (let i = 1; i < src.length; i++) {
        const a = src[i] ?? null;
        const b = src[i - 1] ?? null;
        if (a !== null && b !== null) {
            const d = a - b;
            gains[i] = d > 0 ? d : 0;
            losses[i] = d < 0 ? -d : 0;
        }
    }
    const ag = rma(gains, n);
    const al = rma(losses, n);
    const out: Ser = new Array(src.length).fill(null);
    for (let i = 0; i < src.length; i++) {
        const g = ag[i] ?? null;
        const l = al[i] ?? null;
        if (g !== null && l !== null) {
            out[i] = g + l === 0 ? 50 : 100 - 100 / (1 + g / (l || 1e-12));
        }
    }
    return out;
}

// true range — 需要 high/low/close 三條序列
export function tr(highIn: SerIn, lowIn: SerIn, closeIn: SerIn): Ser {
    const h = toSer(highIn);
    const l = toSer(lowIn);
    const c = toSer(closeIn);
    const out: Ser = new Array(h.length).fill(null);
    for (let i = 0; i < h.length; i++) {
        const hi = h[i] ?? null;
        const lo = l[i] ?? null;
        if (hi === null || lo === null) continue;
        const pc = (i > 0 ? c[i - 1] : null) ?? null;
        out[i] =
            pc === null
                ? hi - lo
                : Math.max(hi - lo, Math.abs(hi - pc), Math.abs(lo - pc));
    }
    return out;
}

export const atr = (
    high: SerIn,
    low: SerIn,
    close: SerIn,
    n: number,
): Ser => rma(tr(high, low, close), n);

// ---- elementwise arithmetic（序列或常數混用）----

type Operand = SerIn | number;

function bin(a: Operand, b: Operand, f: (x: number, y: number) => number): Ser {
    const sa = typeof a === 'number' ? null : toSer(a);
    const sb = typeof b === 'number' ? null : toSer(b);
    const len = Math.max(sa?.length ?? 0, sb?.length ?? 0);
    const out: Ser = new Array(len).fill(null);
    for (let i = 0; i < len; i++) {
        const x = sa ? sa[i] : (a as number);
        const y = sb ? sb[i] : (b as number);
        if (x !== null && x !== undefined && y !== null && y !== undefined) {
            const v = f(x, y);
            out[i] = Number.isFinite(v) ? v : null;
        }
    }
    return out;
}

export const add = (a: Operand, b: Operand): Ser => bin(a, b, (x, y) => x + y);
export const sub = (a: Operand, b: Operand): Ser => bin(a, b, (x, y) => x - y);
export const mul = (a: Operand, b: Operand): Ser => bin(a, b, (x, y) => x * y);
export const div = (a: Operand, b: Operand): Ser =>
    bin(a, b, (x, y) => (y === 0 ? NaN : x / y));
export const max = (a: Operand, b: Operand): Ser => bin(a, b, Math.max);
export const min = (a: Operand, b: Operand): Ser => bin(a, b, Math.min);
export const avg = (a: Operand, b: Operand): Ser =>
    bin(a, b, (x, y) => (x + y) / 2);

export function abs(src: SerIn): Ser {
    return toSer(src).map((v) => (v === null ? null : Math.abs(v)));
}

// 往回取 n 期前的值（ref）：out[i] = src[i - n]
export function offset(srcIn: SerIn, n: number): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    n = Math.floor(n);
    for (let i = 0; i < src.length; i++) {
        const j = i - n;
        if (j >= 0 && j < src.length) out[i] = src[j] ?? null;
    }
    return out;
}

// 累積和（OBV 這類自己動手算時好用）
export function cum(srcIn: SerIn): Ser {
    const src = toSer(srcIn);
    const out: Ser = new Array(src.length).fill(null);
    let acc = 0;
    let seen = false;
    for (let i = 0; i < src.length; i++) {
        const v = src[i] ?? null;
        if (v !== null) {
            acc += v;
            seen = true;
        }
        if (seen) out[i] = acc;
    }
    return out;
}

// 黃金交叉/死亡交叉：發生的 K 棒 = 1，否則 0（b 可為常數水平）
export function crossover(a: Operand, b: Operand): Ser {
    return crossImpl(a, b, 1);
}
export function crossunder(a: Operand, b: Operand): Ser {
    return crossImpl(a, b, -1);
}
function crossImpl(a: Operand, b: Operand, dir: 1 | -1): Ser {
    const diff = sub(a, b);
    const out: Ser = new Array(diff.length).fill(null);
    for (let i = 1; i < diff.length; i++) {
        const d = diff[i] ?? null;
        const pd = diff[i - 1] ?? null;
        if (d !== null && pd !== null) {
            out[i] = d * dir > 0 && pd * dir <= 0 ? 1 : 0;
        }
    }
    return out;
}
