// src/lib/option-pick.ts — broadcast an option/future code picked from the
// 選擇權 T 字 so the 組合單 panel can drop it into a leg (issue #1). Separate
// from the global symbol-select so only a combo panel in 連動 mode consumes
// it. Bridged across windows (popouts) with BroadcastChannel so a popped-out
// T 字 still fills a combo panel in the main window and vice versa.

import { useSyncExternalStore } from 'react';

let current: { code: string; seq: number } | null = null;
const listeners = new Set<() => void>();

const channel =
    typeof BroadcastChannel !== 'undefined'
        ? new BroadcastChannel('sj-opt-pick')
        : null;

function apply(code: string) {
    current = { code, seq: (current?.seq ?? 0) + 1 };
    listeners.forEach((l) => l());
}

channel?.addEventListener('message', (e) => {
    if (typeof e.data === 'string' && e.data) apply(e.data);
});

export function pickOptionLeg(code: string) {
    apply(code);
    channel?.postMessage(code);
}

export function useOptionLegPick(): { code: string; seq: number } | null {
    return useSyncExternalStore(
        (l) => {
            listeners.add(l);
            return () => listeners.delete(l);
        },
        () => current,
    );
}

// ---- cross-window global symbol select（popout T 字 → 主視窗下單面板）----
// popout windows are pinned to their own code, so a pick there must ask the
// MAIN window to switch its selected symbol (下單面板/五檔/K 線 follow it).
const selectChannel =
    typeof BroadcastChannel !== 'undefined'
        ? new BroadcastChannel('sj-select-code')
        : null;

export function broadcastSelectCode(code: string) {
    selectChannel?.postMessage(code);
}

export function onBroadcastSelectCode(handler: (code: string) => void) {
    if (!selectChannel) return () => undefined;
    const fn = (e: MessageEvent) => {
        if (typeof e.data === 'string' && e.data) handler(e.data);
    };
    selectChannel.addEventListener('message', fn);
    return () => selectChannel.removeEventListener('message', fn);
}
