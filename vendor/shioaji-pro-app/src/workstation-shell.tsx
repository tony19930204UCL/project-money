import { useEffect, useMemo, useState } from 'react';
import App from './App';
import './workstation-shell.css';

type WorkspaceKey =
    | 'command-center'
    | 'markets'
    | 'chart'
    | 'paper-trade'
    | 'portfolio'
    | 'strategy-lab'
    | 'research'
    | 'replay'
    | 'risk'
    | 'diagnostics';

type LoadStatus = 'loading' | 'ok' | 'empty' | 'stale' | 'error';

type ViewDefinition = {
    key: WorkspaceKey;
    label: string;
    eyebrow: string;
    title: string;
    endpoint?: string;
    description: string;
};

const VIEWS: ViewDefinition[] = [
    {
        key: 'command-center',
        label: 'Command Center',
        eyebrow: 'TRADING TERMINAL',
        title: 'Command Center',
        description: 'The existing trading terminal remains the primary operating surface.',
    },
    {
        key: 'markets',
        label: 'Markets',
        eyebrow: 'MARKET OBSERVER',
        title: 'Markets',
        endpoint: '/api/watchlists',
        description: 'Watchlists and source quality from the existing market-data contract.',
    },
    {
        key: 'chart',
        label: 'Chart',
        eyebrow: 'PRICE CONTEXT',
        title: 'Chart',
        endpoint: '/api/market/bars/2330.TW?timeframe=1D&limit=20',
        description: 'Recent bars for the default instrument using the existing bars endpoint.',
    },
    {
        key: 'paper-trade',
        label: 'Paper Trade',
        eyebrow: 'PAPER ONLY',
        title: 'Paper Trade',
        endpoint: '/api/paper/orders',
        description: 'Read-only view of the local paper-order ledger. Mutations are not performed by navigation.',
    },
    {
        key: 'portfolio',
        label: 'Portfolio',
        eyebrow: 'SIMULATED LEDGERS',
        title: 'Portfolio',
        endpoint: '/api/portfolio',
        description: 'Swing and intraday paper ledgers from the canonical portfolio API.',
    },
    {
        key: 'strategy-lab',
        label: 'Strategy Lab',
        eyebrow: 'REGISTRY',
        title: 'Strategy Lab',
        endpoint: '/api/strategies',
        description: 'Discovered strategy registry. Activation and mutation controls remain outside this observer shell.',
    },
    {
        key: 'research',
        label: 'Research',
        eyebrow: 'QUARANTINE',
        title: 'Research',
        endpoint: '/api/research/inbox',
        description: 'Canonical research inbox. Unverified research is not promoted into order authority.',
    },
    {
        key: 'replay',
        label: 'Replay',
        eyebrow: 'EXPERIMENT HISTORY',
        title: 'Replay',
        endpoint: '/api/paper/experiments',
        description: 'Recorded replay/experiment summaries exposed by the existing experiments contract.',
    },
    {
        key: 'risk',
        label: 'Risk',
        eyebrow: 'GUARDRAILS',
        title: 'Risk',
        endpoint: '/api/paper/risk-limits',
        description: 'Current paper risk limits. This view is observational and cannot change limits.',
    },
    {
        key: 'diagnostics',
        label: 'Diagnostics',
        eyebrow: 'SYSTEM TRUTH',
        title: 'Diagnostics',
        endpoint: '/api/diagnostics',
        description: 'Runtime invariants and adapter readiness from the diagnostics endpoint.',
    },
];

function summarize(value: unknown): string {
    if (Array.isArray(value)) {
        if (value.length === 0) return 'No records currently available.';
        return JSON.stringify(value.slice(0, 5), null, 2);
    }
    if (value && typeof value === 'object') {
        return JSON.stringify(value, null, 2);
    }
    return String(value ?? 'No data');
}

function classify(value: unknown): LoadStatus {
    if (Array.isArray(value) && value.length === 0) return 'empty';
    if (value && typeof value === 'object') {
        const record = value as Record<string, unknown>;
        if (record.data_status === 'unavailable') return 'empty';
        if (
            record.is_stale === true ||
            record.data_status === 'stale' ||
            (Array.isArray(record.bars) &&
                record.bars.some(
                    (bar) =>
                        bar &&
                        typeof bar === 'object' &&
                        (bar as Record<string, unknown>).is_stale === true,
                ))
        ) {
            return 'stale';
        }
    }
    return 'ok';
}


type PaperOrderRow = {
    order_id: string;
    origin: string;
    strategy_id?: string | null;
    strategy_version?: string | null;
    symbol: string;
    quantity: number;
    status: string;
};

type PaperFillRow = {
    fill_id: string;
    order_id: string;
    symbol: string;
    quantity: number;
    fill_price: number;
};

function PaperTradeWorkspace({ definition }: { definition: ViewDefinition }) {
    const [status, setStatus] = useState<LoadStatus>('loading');
    const [orders, setOrders] = useState<PaperOrderRow[]>([]);
    const [fills, setFills] = useState<PaperFillRow[]>([]);
    const [portfolio, setPortfolio] = useState<unknown>(null);
    const [quantity, setQuantity] = useState('1');
    const [limitPrice, setLimitPrice] = useState('101');
    const [reason, setReason] = useState('manual paper order');
    const [orderType, setOrderType] = useState<'LIMIT' | 'MARKET'>('LIMIT');
    const [dataState, setDataState] = useState<'fresh' | 'stale' | 'noquote'>('fresh');
    const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
    const [actionMessage, setActionMessage] = useState('');
    const [busy, setBusy] = useState(false);

    const refresh = async () => {
        const [ordersResponse, fillsResponse, portfolioResponse] = await Promise.all([
            fetch('/api/paper/orders', { cache: 'no-store' }),
            fetch('/api/fills', { cache: 'no-store' }),
            fetch('/api/portfolio', { cache: 'no-store' }),
        ]);
        if (!ordersResponse.ok || !fillsResponse.ok || !portfolioResponse.ok) {
            throw new Error('Paper readback unavailable');
        }
        const nextOrders = await ordersResponse.json() as PaperOrderRow[];
        const nextFills = await fillsResponse.json() as PaperFillRow[];
        const nextPortfolio = await portfolioResponse.json();
        setOrders(nextOrders);
        setFills(nextFills);
        setPortfolio(nextPortfolio);
        setStatus(nextOrders.length ? 'ok' : 'empty');
    };

    useEffect(() => {
        refresh().catch((error: unknown) => {
            setStatus('error');
            setActionMessage(error instanceof Error ? error.message : 'Paper readback failed');
        });
    }, []);

    const payload = () => {
        const qty = Number(quantity);
        const px = Number(limitPrice);
        const noQuote = dataState === 'noquote';
        return {
            currency: 'TWD',
            symbol: '2330.TW',
            market: 'TW',
            bucket: 'swing',
            side: 'BUY',
            order_type: orderType,
            quantity: qty,
            limit_price: orderType === 'LIMIT' && Number.isFinite(px) && px > 0 ? px : null,
            origin: 'MANUAL',
            reason,
            explicit_user_instruction: true,
            audit_metadata: { ui_surface: 'paper-trade', testable_contract: true },
            data: {
                source: 'ui-paper-order',
                age_seconds: dataState === 'stale' ? 3600 : 0,
                last_price: noQuote ? null : 100,
                is_stale: dataState === 'stale',
                is_fallback: false,
            },
        };
    };

    const postJson = async (url: string, body?: unknown) => {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data);
            throw new Error(detail || ('HTTP ' + response.status));
        }
        return data;
    };

    const runPreview = async () => {
        setBusy(true);
        setActionMessage('');
        setPreview(null);
        try {
            const response = await fetch('/api/paper/orders/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload()),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data);
                throw new Error(detail || ('HTTP ' + response.status));
            }
            setPreview(data);
            setActionMessage(String(data.status ?? 'PREVIEWED'));
        } catch (error) {
            setActionMessage(error instanceof Error ? error.message : 'Preview failed');
        } finally {
            setBusy(false);
        }
    };

    const confirm = async () => {
        setBusy(true);
        try {
            const result = await postJson('/api/paper/orders', payload());
            setActionMessage(('CONFIRMED ' + (result.order?.order_id ?? '')).trim());
            setPreview(null);
            await refresh();
        } catch (error) {
            setActionMessage(error instanceof Error ? error.message : 'Confirm failed');
        } finally {
            setBusy(false);
        }
    };

    const cancel = async (orderId: string) => {
        setBusy(true);
        try {
            await postJson('/api/paper/orders/' + encodeURIComponent(orderId) + '/cancel');
            setActionMessage('CANCELLED ' + orderId);
            await refresh();
        } catch (error) {
            setActionMessage(error instanceof Error ? error.message : 'Cancel failed');
        } finally {
            setBusy(false);
        }
    };

    const replace = async (orderId: string) => {
        setBusy(true);
        try {
            const result = await postJson(
                '/api/paper/orders/' + encodeURIComponent(orderId) + '/cancel-replace',
                payload(),
            );
            setActionMessage('REPLACED ' + orderId + ' -> ' + (result.order?.order_id ?? ''));
            await refresh();
        } catch (error) {
            setActionMessage(error instanceof Error ? error.message : 'Replace failed');
        } finally {
            setBusy(false);
        }
    };

    const resetPreview = () => {
        setPreview(null);
        setActionMessage('');
    };

    return (
        <section className="pm-workspace-panel" data-workspace-view="paper-trade" data-api-status={status}>
            <header className="pm-workspace-header">
                <div>
                    <span className="pm-eyebrow">{definition.eyebrow}</span>
                    <h1>{definition.title}</h1>
                    <p>Preview is non-mutating. Confirm, cancel and replace are explicit paper-only operator actions.</p>
                </div>
                <span className={'pm-status pm-status-' + status}>{status.toUpperCase()}</span>
            </header>

            <div className="pm-order-panel" aria-label="Manual paper order controls">
                <label>Quantity
                    <input aria-label="Paper quantity" value={quantity}
                        onChange={(event) => { setQuantity(event.target.value); resetPreview(); }} />
                </label>
                <label>Order type
                    <select aria-label="Paper order type" value={orderType}
                        onChange={(event) => { setOrderType(event.target.value as 'LIMIT' | 'MARKET'); resetPreview(); }}>
                        <option value="LIMIT">LIMIT</option>
                        <option value="MARKET">MARKET</option>
                    </select>
                </label>
                <label>Limit price
                    <input aria-label="Paper limit price" value={limitPrice} disabled={orderType !== 'LIMIT'}
                        onChange={(event) => { setLimitPrice(event.target.value); resetPreview(); }} />
                </label>
                <label>Reason
                    <input aria-label="Paper reason" value={reason}
                        onChange={(event) => { setReason(event.target.value); resetPreview(); }} />
                </label>
                <label>Data state
                    <select aria-label="Paper data state" value={dataState}
                        onChange={(event) => { setDataState(event.target.value as 'fresh' | 'stale' | 'noquote'); resetPreview(); }}>
                        <option value="fresh">FRESH</option>
                        <option value="stale">STALE</option>
                        <option value="noquote">NO_QUOTE</option>
                    </select>
                </label>
                <div className="pm-order-actions">
                    <button type="button" onClick={runPreview} disabled={busy}>Preview order</button>
                    <button type="button" onClick={confirm} disabled={busy || preview?.status !== 'APPROVED'}>
                        Confirm paper order
                    </button>
                </div>
                <div className="pm-order-feedback" role="status">{actionMessage}</div>
                {preview ? <pre data-testid="paper-preview">{summarize(preview)}</pre> : null}
            </div>

            <div className="pm-order-readback" aria-label="Canonical paper order readback">
                <table>
                    <thead><tr><th>Order</th><th>Origin</th><th>Version</th><th>Status</th><th>Fills</th><th>Actions</th></tr></thead>
                    <tbody>
                        {orders.map((order) => {
                            const orderFills = fills.filter((fill) => fill.order_id === order.order_id);
                            return (
                                <tr key={order.order_id} data-order-id={order.order_id}>
                                    <td>{order.order_id}</td><td>{order.origin}</td>
                                    <td>{order.strategy_version ?? '-'}</td><td>{order.status}</td>
                                    <td>{orderFills.map((fill) => fill.fill_id + ':' + fill.quantity + '@' + fill.fill_price).join(', ') || '-'}</td>
                                    <td>
                                        {order.status === 'PENDING' || order.status === 'PARTIALLY_FILLED' ? (
                                            <>
                                                <button type="button" onClick={() => cancel(order.order_id)} disabled={busy}>Cancel</button>
                                                <button type="button" onClick={() => replace(order.order_id)} disabled={busy}>Replace</button>
                                            </>
                                        ) : null}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            <div className="pm-data-card" data-state-kind={status === 'loading' ? undefined : status}>
                <div className="pm-data-card-head">
                    <strong>/api/paper/orders + /api/fills + /api/portfolio</strong>
                    <span>Canonical paper-only readback.</span>
                </div>
                <pre>{summarize({ orders, fills, portfolio })}</pre>
            </div>
        </section>
    );
}

function DataWorkspace({ definition }: { definition: ViewDefinition }) {
    const [status, setStatus] = useState<LoadStatus>('loading');
    const [payload, setPayload] = useState<unknown>(null);
    const [message, setMessage] = useState('Loading local API state…');

    useEffect(() => {
        if (!definition.endpoint) return;
        const controller = new AbortController();
        setStatus('loading');
        setMessage('Loading local API state…');
        fetch(definition.endpoint, { cache: 'no-store', signal: controller.signal })
            .then(async (response) => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const body: unknown = await response.json();
                setPayload(body);
                const next = classify(body);
                setStatus(next);
                setMessage(next === 'empty' ? 'The endpoint returned no current records.' : 'Loaded from the local FastAPI contract.');
            })
            .catch((error: unknown) => {
                if (controller.signal.aborted) return;
                setStatus('error');
                setMessage(error instanceof Error ? error.message : 'Local API read failed.');
            });
        return () => controller.abort();
    }, [definition.endpoint]);

    return (
        <section className="pm-workspace-panel" data-workspace-view={definition.key} data-api-status={status}>
            <header className="pm-workspace-header">
                <div>
                    <span className="pm-eyebrow">{definition.eyebrow}</span>
                    <h1>{definition.title}</h1>
                    <p>{definition.description}</p>
                </div>
                <span className={`pm-status pm-status-${status}`}>{status.toUpperCase()}</span>
            </header>

            <div className="pm-data-card" data-state-kind={status === 'loading' ? undefined : status}>
                <div className="pm-data-card-head">
                    <strong>{definition.endpoint}</strong>
                    <span>{message}</span>
                </div>
                <pre>{summarize(payload)}</pre>
            </div>
        </section>
    );
}

export function WorkstationShell() {
    const [active, setActive] = useState<WorkspaceKey>('command-center');
    const [healthStatus, setHealthStatus] = useState<LoadStatus>('loading');
    const [healthPayload, setHealthPayload] = useState<unknown>(null);
    const [terminalOpen, setTerminalOpen] = useState(false);
    useEffect(() => {
        const controller = new AbortController();
        fetch('/api/health', { cache: 'no-store', signal: controller.signal })
            .then((response) => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json();
            })
            .then((body) => {
                setHealthPayload(body);
                setHealthStatus('ok');
            })
            .catch(() => {
                if (!controller.signal.aborted) setHealthStatus('error');
            });
        return () => controller.abort();
    }, []);
    const definition = useMemo(
        () => VIEWS.find((item) => item.key === active) ?? VIEWS[0],
        [active],
    );

    return (
        <div className="pm-workstation-shell">
            <header className="pm-shell-topbar">
                <div>
                    <span className="pm-shell-brand">PROJECT MONEY</span>
                    <span className="pm-shell-mode">PAPER / LOCAL</span>
                </div>
                <span className="pm-shell-note">No broker route · observer navigation is GET-only</span>
            </header>
            <nav className="pm-workstation-nav" aria-label="Project Money workstation">
                {VIEWS.map((item) => (
                    <button
                        key={item.key}
                        type="button"
                        className={active === item.key ? 'active' : ''}
                        aria-pressed={active === item.key}
                        onClick={() => setActive(item.key)}
                    >
                        {item.label}
                    </button>
                ))}
            </nav>
            <main className="pm-workstation-main">
                {definition.key === 'command-center' ? (
                    <section
                        className="pm-workspace-panel pm-command-center"
                        data-workspace-view="command-center"
                        data-api-status={healthStatus}
                    >
                        <div className="pm-command-center-intro">
                            <span className="pm-eyebrow">TRADING TERMINAL</span>
                            <h1>Command Center</h1>
                            <p>
                                The existing vendor trading terminal remains available, but observer
                                navigation does not start its subscription/data POST traffic.
                            </p>
                            <div className="pm-data-card" data-state-kind={healthStatus === 'loading' ? undefined : healthStatus}>
                                <div className="pm-data-card-head">
                                    <strong>/api/health</strong>
                                    <span>Loaded from the local FastAPI contract.</span>
                                </div>
                                <pre>{summarize(healthPayload)}</pre>
                            </div>
                            {!terminalOpen ? (
                                <button
                                    type="button"
                                    className="pm-open-terminal"
                                    onClick={() => setTerminalOpen(true)}
                                >
                                    Open Trading Terminal
                                </button>
                            ) : null}
                        </div>
                        {terminalOpen ? (
                            <div className="pm-existing-terminal">
                                <App />
                            </div>
                        ) : null}
                    </section>
                ) : definition.key === 'paper-trade' ? (
                    <PaperTradeWorkspace definition={definition} />
                ) : (
                    <DataWorkspace definition={definition} />
                )}
            </main>
        </div>
    );
}
