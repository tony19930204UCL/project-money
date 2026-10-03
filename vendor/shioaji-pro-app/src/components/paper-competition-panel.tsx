import { useEffect, useMemo, useState } from 'react';
import { cioApi } from '../lib/cio-api';

interface CompetitionRow {
    rank: number;
    strategy_id: string;
    name: string;
    style?: string;
    market: string;
    base_currency: string;
    initial_cash: number;
    equity: number;
    pnl: number;
    return_pct: number;
    cash: number;
    open_positions: number;
    equity_reporting: number;
    pnl_reporting: number;
    cash_reporting: number;
}

interface CompetitionSnapshot {
    paper_only: boolean;
    broker_connected: boolean;
    autonomous_capital_decisions: boolean;
    competition: { name: string; start: string; end: string; days_remaining: number; reporting_currency?: string };
    total: { currency?: string; initial_cash: number; equity: number; pnl: number; return_pct: number };
    leaderboard: CompetitionRow[];
    recent_actions: Array<Record<string, unknown>>;
}

const EMPTY: CompetitionSnapshot = {
    paper_only: true,
    broker_connected: false,
    autonomous_capital_decisions: false,
    competition: { name: 'TW + US Autonomous Paper Team Competition', start: '', end: '', days_remaining: 0, reporting_currency: 'TWD' },
    total: { currency: 'TWD', initial_cash: 0, equity: 0, pnl: 0, return_pct: 0 },
    leaderboard: [],
    recent_actions: [],
};

function money(value: number) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function nativeMoney(value: number, currency: string) {
    return `${currency} ${money(value)}`;
}

function date(value: string) {
    if (!value) return '—';
    return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function pct(value: number) {
    return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function actionLabel(action: Record<string, unknown>) {
    const strategy = String(action.strategy_id ?? action.strategy ?? 'strategy');
    const verb = String(action.action ?? action.decision ?? action.reason ?? 'updated');
    const symbol = action.symbol ?? action.code ?? action.ticker;
    return `${strategy} · ${verb}${symbol ? ` · ${String(symbol)}` : ''}`;
}

export function PaperCompetitionPanel() {
    const [snapshot, setSnapshot] = useState<CompetitionSnapshot>(EMPTY);
    const [error, setError] = useState<string | null>(null);
    const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

    useEffect(() => {
        let disposed = false;
        const refresh = async () => {
            try {
                const next = await cioApi.paperCompetition();
                if (!disposed) {
                    setSnapshot(next);
                    setError(null);
                    setUpdatedAt(new Date());
                }
            } catch (cause) {
                if (!disposed) setError(cause instanceof Error ? cause.message : 'competition endpoint unavailable');
            }
        };
        void refresh();
        const timer = window.setInterval(() => void refresh(), 30_000);
        return () => { disposed = true; window.clearInterval(timer); };
    }, []);

    const safety = useMemo(() => {
        if (snapshot.paper_only && !snapshot.broker_connected) return 'PAPER ONLY · NO BROKER';
        return 'SAFETY CHECK REQUIRED';
    }, [snapshot.paper_only, snapshot.broker_connected]);

    return (
        <section style={{ display: 'flex', flexDirection: 'column', gap: 10, height: '100%', minWidth: 0, color: 'var(--sj-text, #d7dee8)', fontSize: 12 }}>
            <header style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                <div>
                    <strong style={{ fontSize: 14 }}>{snapshot.competition.name}</strong>
                    <div style={{ color: 'var(--sj-muted, #8d99a8)', marginTop: 3 }}>{date(snapshot.competition.start)} – {date(snapshot.competition.end)} · {snapshot.competition.days_remaining} days remaining</div>
                </div>
                <span style={{ color: '#f6c453', border: '1px solid #765d20', padding: '4px 7px', borderRadius: 4, fontSize: 10, letterSpacing: '.04em', whiteSpace: 'nowrap' }}>{safety}</span>
            </header>
            {error && <div style={{ color: '#ff8c8c', border: '1px solid #713d46', padding: '6px 8px', borderRadius: 4 }}>Unable to refresh: {error}</div>}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 6 }}>
                {[
                    ['Initial', snapshot.total.initial_cash],
                    ['Equity', snapshot.total.equity],
                    ['P&L', snapshot.total.pnl],
                    ['Return', snapshot.total.return_pct, true],
                ].map(([label, value, isPct]) => (
                    <div key={String(label)} style={{ background: 'rgba(255,255,255,.035)', border: '1px solid rgba(255,255,255,.08)', borderRadius: 4, padding: '7px 8px', minWidth: 0 }}>
                        <div style={{ color: 'var(--sj-muted, #8d99a8)', fontSize: 10 }}>{label}{!isPct ? ` · ${snapshot.total.currency ?? 'TWD'}` : ''}</div>
                        <strong style={{ display: 'block', marginTop: 3, color: Number(value) >= 0 ? '#8bd6a5' : '#ff8c8c', overflow: 'hidden', textOverflow: 'ellipsis' }}>{isPct ? pct(Number(value)) : money(Number(value))}</strong>
                    </div>
                ))}
            </div>
            <div style={{ overflow: 'auto', flex: '1 1 auto', minHeight: 80 }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', whiteSpace: 'nowrap' }}>
                    <thead><tr style={{ color: 'var(--sj-muted, #8d99a8)', textAlign: 'right' }}>
                        <th style={{ textAlign: 'left', padding: '4px 5px' }}># Strategy</th><th>Market / style</th><th>NAV (TWD)</th><th>P&L (TWD)</th><th>Return</th><th>Native cash</th><th>Pos</th>
                    </tr></thead>
                    <tbody>{snapshot.leaderboard.map((row) => <tr key={row.strategy_id} style={{ borderTop: '1px solid rgba(255,255,255,.07)', textAlign: 'right' }}>
                        <td style={{ textAlign: 'left', padding: '6px 5px' }}><b>{row.rank}. {row.name || row.strategy_id}</b><div style={{ color: 'var(--sj-muted, #8d99a8)', fontSize: 10 }}>{row.strategy_id}</div></td>
                        <td>{row.market} / {row.style ?? 'paper'}<div style={{ color: '#8bd6a5', fontSize: 10 }}>running</div></td><td>{money(row.equity_reporting)}</td><td style={{ color: row.pnl_reporting >= 0 ? '#8bd6a5' : '#ff8c8c' }}>{money(row.pnl_reporting)}</td><td style={{ color: row.return_pct >= 0 ? '#8bd6a5' : '#ff8c8c' }}>{pct(row.return_pct)}</td><td>{nativeMoney(row.cash, row.base_currency)}</td><td>{row.open_positions}</td>
                    </tr>)}</tbody>
                </table>
                {!snapshot.leaderboard.length && <div style={{ padding: 16, textAlign: 'center', color: 'var(--sj-muted, #8d99a8)' }}>No TW/US paper strategies reported yet.</div>}
            </div>
            <footer style={{ borderTop: '1px solid rgba(255,255,255,.08)', paddingTop: 7 }}>
                <div style={{ color: 'var(--sj-muted, #8d99a8)', marginBottom: 4 }}>Recent actions {updatedAt ? `· updated ${updatedAt.toLocaleTimeString()}` : ''}</div>
                {snapshot.recent_actions.slice(-4).reverse().map((action, index) => <div key={`${String(action.run_id ?? action.timestamp ?? index)}-${index}`} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.6 }}>{actionLabel(action)}</div>)}
                {!snapshot.recent_actions.length && <div style={{ color: 'var(--sj-muted, #8d99a8)' }}>No recent actions.</div>}
            </footer>
        </section>
    );
}
