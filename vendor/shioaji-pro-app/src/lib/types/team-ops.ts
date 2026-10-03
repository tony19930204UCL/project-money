// src/lib/types/team-ops.ts
// Observer-first Team Operations Data Models

export type FreshnessStatus = 'fresh' | 'stale' | 'unavailable';

export type NavStatus = 'fresh' | 'stale' | 'unavailable' | 'NAV_UNAVAILABLE';

export interface SafetyStatus {
    paper_only: boolean | null;
    broker_connected: boolean | null;
    broker_state: 'DISCONNECTED' | 'CONNECTED' | string | null;
    autonomous_capital_decisions: boolean | null;
    kill_switch_active: boolean | null;
}

export interface SessionInfo {
    market: 'TW' | 'US';
    status: 'OPEN' | 'CLOSED' | 'PRE_MARKET' | 'POST_MARKET' | string;
    session_label: string;
    timezone: string;
    server_time: string;
}

export interface TeamPortfolioSummary {
    reporting_currency: string;
    equity: number | null;
    nav_status: NavStatus;
    cash: number | null;
    realized_pnl: number | null;
    unrealized_pnl: number | null;
    return_pct: number | null;
    initial_cash: number | null;
    as_of: string | null;
}

export interface TeamHolding {
    symbol: string;
    name?: string;
    market: 'TW' | 'US' | string;
    quantity: number;
    currency: string;
    entry_price: number | null;
    current_price: number | null;
    market_value: number | null;
    unrealized_pnl: number | null;
    return_pct: number | null;
    price_freshness: FreshnessStatus;
    quote_timestamp?: string | null;
    strategy_id?: string;
}

export interface MarketRegimeInfo {
    market: 'TW' | 'US';
    regime: string;
    trend: string;
    volatility: string;
    breadth?: string;
    updated_at: string;
}

export interface FunctionalDeskRole {
    role_id: string;
    role_name: string;
    title: string;
    scope: string;
    status: 'WORKING' | 'MONITORING' | 'STANDBY' | 'ACTIVE' | string;
    current_task: string;
    next_review_time?: string | null;
}

export interface ActivePlaybookInfo {
    playbook_id: string;
    playbook_name: string;
    description?: string;
    selection_rationale: string;
    regime?: string;
    data_quality?: string;
    session_state?: string;
    gross_exposure?: number;
    target_cash_pct?: number;
    risk_multiplier?: number;
    selected_at: string;
    next_review_time: string;
}

export interface DeskStatus {
    desk_id: string;
    desk_name: string;
    active_playbook: ActivePlaybookInfo;
    roles: FunctionalDeskRole[];
    next_review_time: string;
}

export interface TeamPosture {
    posture: 'DEFENSIVE' | 'NEUTRAL' | 'AGGRESSIVE' | 'CAPITAL_PRESERVATION' | string;
    posture_label: string;
    rationale: string;
    tw_regime: MarketRegimeInfo;
    us_regime: MarketRegimeInfo;
    target_cash_pct?: number;
    updated_at: string;
    risk_budget_multiplier?: number;
    active_playbook?: ActivePlaybookInfo;
    desk?: DeskStatus;
    next_review_time?: string;
}

export interface RiskBreach {
    id: string;
    rule: string;
    level: 'warning' | 'breach' | 'info';
    detail: string;
    timestamp: string;
}

export interface RiskStatus {
    kill_switch_active: boolean | null;
    kill_switch_armed: boolean | null;
    budget_usage_pct: number | null;
    max_drawdown_pct: number | null;
    drawdown_limit_pct: number | null;
    daily_loss_limit: number | null;
    current_daily_loss: number | null;
    var_95_pct?: number | null;
    gross_exposure?: number | null;
    net_exposure?: number | null;
    breaches: RiskBreach[];
}

export interface QuoteItem {
    symbol: string;
    name?: string;
    market: 'TW' | 'US' | string;
    price: number | null;
    change_pct: number | null;
    source: string;
    timestamp: string | null;
    freshness: FreshnessStatus;
}

export interface PaperOrder {
    order_id: string;
    symbol: string;
    market: 'TW' | 'US' | string;
    side: 'BUY' | 'SELL';
    quantity: number;
    price: number | null;
    order_type: 'LIMIT' | 'MARKET' | 'STOP' | string;
    status: 'PENDING' | 'ACCEPTED' | 'WORKING' | 'CANCELLED' | 'REJECTED' | 'FILLED' | string;
    strategy_id?: string;
    rationale?: string;
    created_at: string;
}

export interface PaperFill {
    fill_id: string;
    order_id: string;
    symbol: string;
    market: 'TW' | 'US' | string;
    side: 'BUY' | 'SELL';
    quantity: number;
    price: number;
    fee?: number;
    strategy_id?: string;
    timestamp: string;
}

export interface TeamActivity {
    id: string;
    actor: string;
    status: 'EXECUTED' | 'PROPOSED' | 'REJECTED' | 'MONITORING' | 'ALERT' | string;
    action: string;
    target?: string;
    rationale: string;
    timestamp: string;
}

export interface BenchmarkComparison {
    benchmark_name: string;
    period: string;
    currency: string;
    team_return_pct: number | null;
    benchmark_return_pct: number | null;
    alpha_pct: number | null;
    updated_at: string;
}

export interface StrategyBreakdownRow {
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
    equity_reporting?: number;
    pnl_reporting?: number;
    cash_reporting?: number;
}

export interface TeamOpsSnapshot {
    server_time: string;
    data_freshness: FreshnessStatus;
    safety: SafetyStatus;
    sessions: {
        tw: SessionInfo;
        us: SessionInfo;
    };
    portfolio: TeamPortfolioSummary;
    holdings: TeamHolding[];
    posture: TeamPosture;
    risk: RiskStatus;
    quotes: QuoteItem[];
    orders: PaperOrder[];
    fills: PaperFill[];
    activity: TeamActivity[];
    benchmark: BenchmarkComparison;
    desk?: DeskStatus;
    legacy_leaderboard?: StrategyBreakdownRow[];
    integrity_warnings?: string[];
}
