// src/lib/custom-worker.ts — sandbox for validating user indicator code.
// Runs off the main thread so an infinite loop can be killed by terminate()
// from custom-indicators.ts instead of freezing the UI.

import { runCustom, type BarsInput } from './custom-runtime';

self.onmessage = (
    e: MessageEvent<{
        source: string;
        bars: BarsInput;
        params: Record<string, number>;
    }>,
) => {
    const { source, bars, params } = e.data;
    (self as unknown as Worker).postMessage(runCustom(source, bars, params));
};
