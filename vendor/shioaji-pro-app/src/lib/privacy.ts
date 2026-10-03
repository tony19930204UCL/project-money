// src/lib/privacy.ts — privacy mode masks account identifiers everywhere
// in the UI (screenshots, screen sharing, streaming). Persisted.

import { useSyncExternalStore } from 'react';

const KEY = 'sj-pro-privacy-mode';

let on = false;
try {
    on = localStorage.getItem(KEY) === '1';
} catch {
    // storage unavailable — default off
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function getPrivacyMode(): boolean {
    return on;
}

export function setPrivacyMode(v: boolean) {
    on = v;
    try {
        localStorage.setItem(KEY, v ? '1' : '0');
    } catch {
        // session-only
    }
    listeners.forEach((l) => l());
}

export function usePrivacyMode(): boolean {
    return useSyncExternalStore(subscribe, getPrivacyMode);
}

// 9816502 → •••••02 — keep the tail so multiple accounts stay apart
export function maskAccountId(id: string, priv: boolean): string {
    if (!priv) return id;
    if (id.length <= 2) return '•'.repeat(id.length);
    return '•'.repeat(id.length - 2) + id.slice(-2);
}

export function maskName(name: string, priv: boolean): string {
    return priv ? '•••' : name;
}

// ---- 遮金額 (issue #2): hide amounts in screenshots — separate toggle ----

const MONEY_KEY = 'sj-pro-privacy-money';

let moneyOn = false;
try {
    moneyOn = localStorage.getItem(MONEY_KEY) === '1';
} catch {
    // storage unavailable — default off
}

const moneyListeners = new Set<() => void>();

export function getPrivacyMoney(): boolean {
    return moneyOn;
}

export function setPrivacyMoney(v: boolean) {
    moneyOn = v;
    try {
        localStorage.setItem(MONEY_KEY, v ? '1' : '0');
    } catch {
        // session-only
    }
    moneyListeners.forEach((l) => l());
}

export function usePrivacyMoney(): boolean {
    return useSyncExternalStore((l) => {
        moneyListeners.add(l);
        return () => {
            moneyListeners.delete(l);
        };
    }, getPrivacyMoney);
}

export function maskMoney(text: string, priv: boolean): string {
    return priv ? '•••••' : text;
}
