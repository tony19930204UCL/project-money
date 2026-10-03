// src/lib/toast-prefs.ts — toast size preference (小/標準/大), persisted.

import { useSyncExternalStore } from 'react';

export type ToastScale = 0.9 | 1 | 1.25;

const KEY = 'sj-pro-toast-scale';
const SCALES: ToastScale[] = [0.9, 1, 1.25];

let scale: ToastScale = 1;
try {
    const v = Number(localStorage.getItem(KEY));
    if (SCALES.includes(v as ToastScale)) scale = v as ToastScale;
} catch {
    // default
}

const listeners = new Set<() => void>();

export function getToastScale(): ToastScale {
    return scale;
}

export function setToastScale(v: ToastScale) {
    scale = v;
    try {
        localStorage.setItem(KEY, String(v));
    } catch {
        // session only
    }
    listeners.forEach((l) => l());
}

export function useToastScale(): ToastScale {
    return useSyncExternalStore((l) => {
        listeners.add(l);
        return () => {
            listeners.delete(l);
        };
    }, getToastScale);
}
