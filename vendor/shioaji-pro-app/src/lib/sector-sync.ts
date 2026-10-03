// src/lib/sector-sync.ts — broadcast a "focused sector" so clicking 跳同類
// on a leaderboard row switches the 類股熱力圖 to that stock's sector
// (issue #2). External store like price-sync so only the heatmap re-renders.

import { useSyncExternalStore } from 'react';

let current: { category: string; seq: number } | null = null;
const listeners = new Set<() => void>();

export function focusSector(category: string) {
    current = { category, seq: (current?.seq ?? 0) + 1 };
    listeners.forEach((l) => l());
}

export function useFocusedSector(): { category: string; seq: number } | null {
    return useSyncExternalStore(
        (l) => {
            listeners.add(l);
            return () => listeners.delete(l);
        },
        () => current,
    );
}
