import type { TeamOpsSnapshot } from './types/team-ops';

export interface HealthStatus {
    status: string;
    version: string;
    timestamp: string;
    mode: string;
    paper_only: boolean;
    broker_connected: boolean;
    autonomous_capital_decisions: boolean;
    disclaimer: string;
}

export interface MarketRegime {
    exchange: string;
    status: string;
    freshness: string;
    sample_ticker: string;
    last_price: number;
    change_pct: number;
    source: string;
    market_timestamp: string;
}

export interface PortfolioSummary {
    equity: number;
    cash: number;
    realized_pnl: number;
    open_positions: number;
    open_orders: number;
}

export interface Overview {
    timestamp: string;
    mode: string;
    market_regimes: Record<string, MarketRegime>;
    strategies_summary: Record<string, number>;
    portfolios_summary: Record<string, PortfolioSummary>;
    recent_signals_count: number;
    research_inbox_count: number;
    safety_guards: {
        kill_switch: boolean;
        stale_data_rejection: boolean;
        broker_credentials: string;
    };
}

export interface Portfolio {
    scope: string;
    cash: number;
    equity: number;
    realized_pnl: number;
    unrealized_pnl: number;
    positions: unknown[];
    orders: unknown[];
    fills: unknown[];
}

export interface PortfoliosResponse {
    mode: string;
    paper_only: boolean;
    assumptions: Record<string, number | boolean>;
    swing: Portfolio;
    intraday: Portfolio;
}

export interface Experiment {
    strategy_id: string;
    enabled: boolean;
    universe: string[];
    mode: string;
    cadence_seconds: number;
    expires_at: string;
    [key: string]: unknown;
}

export interface ExperimentRuntimeStatus {
    strategy_id: string;
    enabled: boolean;
    running: boolean;
    processing: boolean;
    current_task: string;
    task_started_at: string | null;
    last_task_status: string | null;
    last_task_completed_at: string | null;
    mode: string;
    decision_schedule: string;
    next_due_at: string | null;
    expires_at: string | null;
    last_run: Record<string, unknown> | null;
}

export interface ChatResult {
    transport: string;
    status: string;
    response: string;
    session_id: string;
}

export interface PaperCompetition {
    paper_only: boolean;
    broker_connected: boolean;
    autonomous_capital_decisions: boolean;
    competition: { name: string; start: string; end: string; days_remaining: number; reporting_currency?: string };
    total: { currency?: string; initial_cash: number; equity: number; pnl: number; return_pct: number };
    leaderboard: Array<{
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
        [key: string]: unknown;
    }>;
    recent_actions: Array<Record<string, unknown>>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(path, {
        ...init,
        headers: {
            'Content-Type': 'application/json',
            ...(init?.headers ?? {}),
        },
    });
    if (!response.ok) {
        const payload = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: string };
        throw new Error(payload.detail ?? `HTTP ${response.status}`);
    }
    return response.json() as Promise<T>;
}

export function validateTeamOpsSnapshot(data: unknown): { valid: true; value: TeamOpsSnapshot } | { valid: false; error: string } {
    if (!data || typeof data !== 'object') {
        return { valid: false, error: 'Snapshot payload must be a non-null object' };
    }
    const d = data as Record<string, unknown>;

    if (typeof d.server_time !== 'string' || !d.server_time.trim()) {
        return { valid: false, error: 'server_time must be a non-empty string' };
    }

    if (typeof d.data_freshness !== 'string' || !['fresh', 'stale', 'unavailable'].includes(d.data_freshness)) {
        return { valid: false, error: `Invalid data_freshness: ${String(d.data_freshness)}` };
    }

    if (!d.safety || typeof d.safety !== 'object') {
        return { valid: false, error: 'safety must be an object' };
    }
    const safety = d.safety as Record<string, unknown>;
    if (typeof safety.paper_only !== 'boolean') {
        return { valid: false, error: 'safety.paper_only must be a boolean' };
    }
    if (typeof safety.broker_connected !== 'boolean') {
        return { valid: false, error: 'safety.broker_connected must be a boolean' };
    }
    if (typeof safety.broker_state !== 'string') {
        return { valid: false, error: 'safety.broker_state must be a string' };
    }
    if (typeof safety.autonomous_capital_decisions !== 'boolean') {
        return { valid: false, error: 'safety.autonomous_capital_decisions must be a boolean' };
    }
    if (typeof safety.kill_switch_active !== 'boolean') {
        return { valid: false, error: 'safety.kill_switch_active must be a boolean' };
    }

    if (!d.sessions || typeof d.sessions !== 'object') {
        return { valid: false, error: 'sessions must be an object' };
    }
    const sessions = d.sessions as Record<string, unknown>;
    if (!sessions.tw || typeof sessions.tw !== 'object') {
        return { valid: false, error: 'sessions.tw must be an object' };
    }
    if (!sessions.us || typeof sessions.us !== 'object') {
        return { valid: false, error: 'sessions.us must be an object' };
    }

    if (!d.portfolio || typeof d.portfolio !== 'object') {
        return { valid: false, error: 'portfolio must be an object' };
    }
    const portfolio = d.portfolio as Record<string, unknown>;
    if (typeof portfolio.reporting_currency !== 'string') {
        return { valid: false, error: 'portfolio.reporting_currency must be a string' };
    }
    if (typeof portfolio.nav_status !== 'string') {
        return { valid: false, error: 'portfolio.nav_status must be a string' };
    }

    if (!Array.isArray(d.holdings)) {
        return { valid: false, error: 'holdings must be an array' };
    }

    if (!d.posture || typeof d.posture !== 'object') {
        return { valid: false, error: 'posture must be an object' };
    }
    const posture = d.posture as Record<string, unknown>;
    if (typeof posture.posture !== 'string') {
        return { valid: false, error: 'posture.posture must be a string' };
    }
    if (!posture.tw_regime || typeof posture.tw_regime !== 'object') {
        return { valid: false, error: 'posture.tw_regime must be an object' };
    }
    if (!posture.us_regime || typeof posture.us_regime !== 'object') {
        return { valid: false, error: 'posture.us_regime must be an object' };
    }

    if (!d.risk || typeof d.risk !== 'object') {
        return { valid: false, error: 'risk must be an object' };
    }
    const risk = d.risk as Record<string, unknown>;
    if (typeof risk.kill_switch_active !== 'boolean') {
        return { valid: false, error: 'risk.kill_switch_active must be a boolean' };
    }
    if (!Array.isArray(risk.breaches)) {
        return { valid: false, error: 'risk.breaches must be an array' };
    }

    if (!Array.isArray(d.quotes)) {
        return { valid: false, error: 'quotes must be an array' };
    }
    if (!Array.isArray(d.orders)) {
        return { valid: false, error: 'orders must be an array' };
    }
    if (!Array.isArray(d.fills)) {
        return { valid: false, error: 'fills must be an array' };
    }
    if (!Array.isArray(d.activity)) {
        return { valid: false, error: 'activity must be an array' };
    }

    if (!d.benchmark || typeof d.benchmark !== 'object') {
        return { valid: false, error: 'benchmark must be an object' };
    }
    const benchmark = d.benchmark as Record<string, unknown>;
    if (typeof benchmark.benchmark_name !== 'string') {
        return { valid: false, error: 'benchmark.benchmark_name must be a string' };
    }
    if (typeof benchmark.period !== 'string') {
        return { valid: false, error: 'benchmark.period must be a string' };
    }
    if (typeof benchmark.currency !== 'string') {
        return { valid: false, error: 'benchmark.currency must be a string' };
    }

    if (d.integrity_warnings !== undefined && !Array.isArray(d.integrity_warnings)) {
        return { valid: false, error: 'integrity_warnings must be an array if provided' };
    }

    return { valid: true, value: d as unknown as TeamOpsSnapshot };
}

export function createUnavailableSnapshot(warning?: string): TeamOpsSnapshot {
    return {
        server_time: '',
        data_freshness: 'unavailable',
        safety: {
            paper_only: null,
            broker_connected: null,
            broker_state: null,
            autonomous_capital_decisions: null,
            kill_switch_active: null,
        },
        sessions: {
            tw: {
                market: 'TW',
                status: 'UNAVAILABLE',
                session_label: '—',
                timezone: 'Asia/Taipei',
                server_time: '',
            },
            us: {
                market: 'US',
                status: 'UNAVAILABLE',
                session_label: '—',
                timezone: 'America/New_York',
                server_time: '',
            },
        },
        portfolio: {
            reporting_currency: '—',
            equity: null,
            nav_status: 'NAV_UNAVAILABLE',
            cash: null,
            realized_pnl: null,
            unrealized_pnl: null,
            return_pct: null,
            initial_cash: null,
            as_of: null,
        },
        holdings: [],
        posture: {
            posture: 'UNAVAILABLE',
            posture_label: '—',
            rationale: '—',
            tw_regime: {
                market: 'TW',
                regime: '—',
                trend: '—',
                volatility: '—',
                updated_at: '',
            },
            us_regime: {
                market: 'US',
                regime: '—',
                trend: '—',
                volatility: '—',
                updated_at: '',
            },
            updated_at: '',
        },
        risk: {
            kill_switch_active: null,
            kill_switch_armed: null,
            budget_usage_pct: null,
            max_drawdown_pct: null,
            drawdown_limit_pct: null,
            daily_loss_limit: null,
            current_daily_loss: null,
            breaches: [],
        },
        quotes: [],
        orders: [],
        fills: [],
        activity: [],
        benchmark: {
            benchmark_name: '—',
            period: '—',
            currency: '—',
            team_return_pct: null,
            benchmark_return_pct: null,
            alpha_pct: null,
            updated_at: '',
        },
        legacy_leaderboard: [],
        integrity_warnings: warning ? [warning] : ['後端端點不可用或資料結構損壞 (Endpoint/Schema Unavailable)'],
    };
}

export async function fetchTeamOpsSnapshot(): Promise<TeamOpsSnapshot> {
    try {
        const direct = await request<unknown>('/api/paper/team-ops');
        const validated = validateTeamOpsSnapshot(direct);
        if (!validated.valid) {
            return createUnavailableSnapshot(`Schema validation failed: ${validated.error}`);
        }
        return validated.value;
    } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return createUnavailableSnapshot(`Endpoint request failed: ${message}`);
    }
}

export const cioApi = {
    health: () => request<HealthStatus>('/api/health'),
    overview: () => request<Overview>('/api/overview'),
    portfolios: () => request<PortfoliosResponse>('/api/portfolios'),
    experiments: () => request<Experiment[]>('/api/paper/experiments'),
    experimentStatus: (strategyId: string) => request<ExperimentRuntimeStatus>(`/api/paper/experiments/${encodeURIComponent(strategyId)}/status`),
    paperCompetition: () => request<PaperCompetition>('/api/paper/competition'),
    teamOps: () => fetchTeamOpsSnapshot(),
    paperOrders: () => request<unknown[]>('/api/paper/orders'),
    signals: () => request<unknown[]>('/api/signals'),
    researchInbox: () => request<unknown[]>('/api/research/inbox'),
    chat: (message: string, sessionId = 'cio-market-lab') => request<ChatResult>('/api/chat', {
        method: 'POST',
        body: JSON.stringify({ message, session_id: sessionId }),
    }),
};

// Safe formatting helpers: never display missing values as zero
export function formatValue(val: number | null | undefined, digits = 2): string {
    if (val === null || val === undefined || Number.isNaN(val)) return '—';
    return val.toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

export function formatPct(val: number | null | undefined, withSign = true): string {
    if (val === null || val === undefined || Number.isNaN(val)) return '—';
    const sign = withSign && val > 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}%`;
}

export function formatCurrency(val: number | null | undefined, currency?: string): string {
    if (val === null || val === undefined || Number.isNaN(val)) return '—';
    if (!currency || currency === '—') return formatValue(val);
    return `${currency} ${formatValue(val)}`;
}
