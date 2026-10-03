// src/lib/polyfills.ts
//
// Lightweight runtime polyfills for older WKWebView (Intel Macs stuck on
// older macOS ship Safari 13–15 era webviews). Build syntax is handled by
// vite's build.target (['es2020','safari13']), but esbuild does not inject
// runtime APIs — these guards cover what our code and deps actually use.
//
// MUST be the first import in src/main.tsx so globals are patched before
// any dependency module evaluates.

/* eslint-disable @typescript-eslint/no-explicit-any */

// structuredClone — Safari 15.4+. Our usages (workspace presets) are plain
// JSON-safe data, so a JSON round-trip is a sufficient fallback.
if (typeof globalThis.structuredClone !== 'function') {
    (globalThis as any).structuredClone = function structuredClone<T>(
        value: T,
    ): T {
        if (value === undefined) return value;
        return JSON.parse(JSON.stringify(value)) as T;
    };
}

// AbortSignal.timeout — Safari 16+. Used by sidecar health probes at boot.
if (
    typeof AbortSignal !== 'undefined' &&
    typeof (AbortSignal as any).timeout !== 'function'
) {
    (AbortSignal as any).timeout = function timeout(ms: number): AbortSignal {
        const controller = new AbortController();
        setTimeout(() => {
            try {
                controller.abort(
                    new DOMException('signal timed out', 'TimeoutError'),
                );
            } catch {
                // older WKWebView: abort() takes no reason argument
                controller.abort();
            }
        }, ms);
        return controller.signal;
    };
}

// Object.hasOwn — Safari 15.4+ (deps may use it).
if (typeof (Object as any).hasOwn !== 'function') {
    (Object as any).hasOwn = function hasOwn(
        obj: object,
        key: PropertyKey,
    ): boolean {
        return Object.prototype.hasOwnProperty.call(obj, key);
    };
}

// String.prototype.replaceAll — Safari 13.1+ (deps may use it).
if (typeof String.prototype.replaceAll !== 'function') {
    Object.defineProperty(String.prototype, 'replaceAll', {
        configurable: true,
        writable: true,
        value: function replaceAll(
            this: string,
            search: string | RegExp,
            replacement: any,
        ): string {
            if (search instanceof RegExp) {
                return this.replace(search, replacement);
            }
            return this.split(String(search)).join(String(replacement));
        },
    });
}

// Array.prototype.at / String.prototype.at — Safari 15.4+ (deps may use it).
function atImpl(this: ArrayLike<unknown> | string, index: number): unknown {
    const len = this.length;
    let i = Math.trunc(index) || 0;
    if (i < 0) i += len;
    if (i < 0 || i >= len) return undefined;
    return this[i];
}
if (typeof (Array.prototype as any).at !== 'function') {
    Object.defineProperty(Array.prototype, 'at', {
        configurable: true,
        writable: true,
        value: atImpl,
    });
}
if (typeof (String.prototype as any).at !== 'function') {
    Object.defineProperty(String.prototype, 'at', {
        configurable: true,
        writable: true,
        value: atImpl,
    });
}

// Array.prototype.findLast / findLastIndex — Safari 15.4+ (deps may use it).
if (typeof (Array.prototype as any).findLast !== 'function') {
    Object.defineProperty(Array.prototype, 'findLast', {
        configurable: true,
        writable: true,
        value: function findLast(
            this: unknown[],
            fn: (v: unknown, i: number, a: unknown[]) => boolean,
            thisArg?: unknown,
        ): unknown {
            for (let i = this.length - 1; i >= 0; i--) {
                if (fn.call(thisArg, this[i], i, this)) return this[i];
            }
            return undefined;
        },
    });
}
if (typeof (Array.prototype as any).findLastIndex !== 'function') {
    Object.defineProperty(Array.prototype, 'findLastIndex', {
        configurable: true,
        writable: true,
        value: function findLastIndex(
            this: unknown[],
            fn: (v: unknown, i: number, a: unknown[]) => boolean,
            thisArg?: unknown,
        ): number {
            for (let i = this.length - 1; i >= 0; i--) {
                if (fn.call(thisArg, this[i], i, this)) return i;
            }
            return -1;
        },
    });
}

// crypto.randomUUID — Safari 15.4+ (deps such as statsig may use it).
if (
    typeof crypto !== 'undefined' &&
    typeof (crypto as any).randomUUID !== 'function' &&
    typeof crypto.getRandomValues === 'function'
) {
    (crypto as any).randomUUID = function randomUUID(): string {
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40; // version 4
        bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80; // variant 10
        const hex: string[] = [];
        for (let i = 0; i < 16; i++) {
            hex.push(((bytes[i] ?? 0) + 0x100).toString(16).slice(1));
        }
        return (
            hex.slice(0, 4).join('') +
            '-' +
            hex.slice(4, 6).join('') +
            '-' +
            hex.slice(6, 8).join('') +
            '-' +
            hex.slice(8, 10).join('') +
            '-' +
            hex.slice(10, 16).join('')
        );
    };
}

// ResizeObserver — Safari 13.1+. Fallback approximates with window resize
// events; imperfect (no element-level granularity) but keeps panels that
// observe their container (depth-map, sparkline, flash-order) from crashing.
if (typeof (globalThis as any).ResizeObserver !== 'function') {
    class ResizeObserverFallback {
        private callback: () => void;
        private targets: Set<Element> = new Set();
        private handler: () => void;
        constructor(callback: (entries: unknown[], observer: unknown) => void) {
            this.callback = () => {
                const entries = Array.from(this.targets, (target) => ({
                    target,
                    contentRect: target.getBoundingClientRect(),
                    borderBoxSize: [],
                    contentBoxSize: [],
                    devicePixelContentBoxSize: [],
                }));
                callback(entries, this);
            };
            this.handler = () => this.callback();
        }
        observe(target: Element): void {
            if (this.targets.size === 0) {
                window.addEventListener('resize', this.handler);
            }
            this.targets.add(target);
            // fire once asynchronously, matching native initial-observe behavior
            setTimeout(this.callback, 0);
        }
        unobserve(target: Element): void {
            this.targets.delete(target);
            if (this.targets.size === 0) {
                window.removeEventListener('resize', this.handler);
            }
        }
        disconnect(): void {
            this.targets.clear();
            window.removeEventListener('resize', this.handler);
        }
    }
    (globalThis as any).ResizeObserver = ResizeObserverFallback;
}
