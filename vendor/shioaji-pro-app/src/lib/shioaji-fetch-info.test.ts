import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const scope = vi.hoisted(() => ({ base: 'http://127.0.0.1:1' }));
vi.mock('./runtime', async importOriginal => ({
    ...(await importOriginal<typeof import('./runtime')>()),
    getApiBase: () => scope.base,
    isTauri: false,
}));
import { fetchInfo, type ServerInfo } from './shioaji';
import { useServerInfo } from './server-info-store';

type Deferred = { resolve: (r: Response) => void; reject: (e: unknown) => void };
const calls: Deferred[] = [];
function call(index: number): Deferred {
    const deferred = calls[index];
    if (!deferred) throw new Error(`no /info request #${index}`);
    return deferred;
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

beforeEach(() => {
    calls.length = 0;
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve, reject) => { calls.push({ resolve, reject }); })));
});
afterEach(() => { vi.unstubAllGlobals(); });

it('fetchInfo publishes responses in request order and keeps caller results unchanged', async () => {
    scope.base = 'http://127.0.0.1:1';
    let seen: ServerInfo | undefined;
    function Probe() { seen = useServerInfo(); return null; }
    let root!: ReturnType<typeof create>;
    await act(async () => { root = create(createElement(Probe)); });
    try {
        const first = fetchInfo();
        const second = fetchInfo();
        expect(calls).toHaveLength(2);
        expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:1/api/v1/info', undefined);
        const info = { name: 'shioaji', version: '1.7.5', description: '', protocols: [], simulation: true };
        await act(async () => { call(1).resolve(json(info)); await second; });
        expect(seen).toEqual(info);
        // Late failure of the first call: caller still sees the error, the store keeps the newer info.
        await act(async () => {
            call(0).resolve(json({ message: 'boom' }, 503));
            await expect(first).rejects.toThrow('503 boom');
        });
        expect(seen).toEqual(info);
        // Base switch: a response for the old server does not land in the new one, but the caller still gets it.
        const stale = fetchInfo();
        scope.base = 'http://127.0.0.1:2';
        await act(async () => { root.update(createElement(Probe)); });
        expect(seen).toBeUndefined();
        const late = { ...info, version: '1.7.6' };
        await act(async () => { call(2).resolve(json(late)); });
        await expect(stale).resolves.toEqual(late);
        expect(seen).toBeUndefined();
    } finally {
        await act(async () => { root.unmount(); });
    }
});
