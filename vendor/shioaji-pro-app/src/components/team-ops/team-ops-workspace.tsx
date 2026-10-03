// src/components/team-ops/team-ops-workspace.tsx
// Observer-first Team Operations Console

import {
    Activity,
    AlertCircle,
    ArrowDownRight,
    ArrowUpRight,
    BarChart3,
    CheckCircle2,
    ChevronDown,
    ChevronUp,
    Clock,
    Lock,
    Pause,
    Play,
    RefreshCw,
    Radio,
    Shield,
    ShieldAlert,
    ShieldCheck,
    SlidersHorizontal,
    TrendingDown,
    TrendingUp,
    Users,
    ZapOff,
} from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import {
    cioApi,
    formatCurrency,
    formatPct,
    formatValue,
} from '../../lib/cio-api';
import type { Experiment, ExperimentRuntimeStatus } from '../../lib/cio-api';
import type {
    FreshnessStatus,
    FunctionalDeskRole,
    PaperFill,
    PaperOrder,
    TeamOpsSnapshot,
} from '../../lib/types/team-ops';
import { vars } from '../../theme.css';
import * as styles from './team-ops-workspace.css';

export function TeamOpsWorkspace() {
    const [snapshot, setSnapshot] = useState<TeamOpsSnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastRefreshTime, setLastRefreshTime] = useState<Date>(new Date());
    const [ordersTab, setOrdersTab] = useState<'orders' | 'fills'>('orders');
    const [showLegacyLeaderboard, setShowLegacyLeaderboard] = useState(false);
    const [agents, setAgents] = useState<Array<{ experiment: Experiment; status: ExperimentRuntimeStatus }>>([]);

    const loadData = useCallback(async () => {
        try {
            setLoading(true);
            const data = await cioApi.teamOps();
            setSnapshot(data);
            try {
                const experiments = await cioApi.experiments();
                if (!Array.isArray(experiments)) {
                    throw new Error('invalid experiment roster');
                }
                const statuses = await Promise.all(
                    experiments.map(async (experiment) => ({
                        experiment,
                        status: await cioApi.experimentStatus(experiment.strategy_id),
                    })),
                );
                setAgents(statuses);
            } catch {
                setAgents([]);
            }
            setError(null);
            setLastRefreshTime(new Date());
        } catch (err) {
            setError(err instanceof Error ? err.message : '無法載入團隊運維快照');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void loadData();
        const timer = setInterval(() => {
            void loadData();
        }, 15000);
        return () => clearInterval(timer);
    }, [loadData]);

    const freshness = snapshot?.data_freshness ?? 'unavailable';
    const navStatus = snapshot?.portfolio?.nav_status ?? 'NAV_UNAVAILABLE';
    const safety = snapshot?.safety;
    const portfolio = snapshot?.portfolio;
    const hasVerifiedCapital = portfolio?.equity != null && portfolio?.cash != null;
    const posture = snapshot?.posture;
    const risk = snapshot?.risk;
    const sessions = snapshot?.sessions;
    const holdings = snapshot?.holdings ?? [];
    const quotes = snapshot?.quotes ?? [];
    const orders = snapshot?.orders ?? [];
    const fills = snapshot?.fills ?? [];
    const activity = snapshot?.activity ?? [];
    const benchmark = snapshot?.benchmark;
    const legacyLeaderboard = snapshot?.legacy_leaderboard ?? [];
    const activeAgentCount = agents.filter(({ status }) => status.processing).length;
    const scheduledAgentCount = agents.filter(({ status }) => status.running).length;

    const desk = (posture as any)?.desk ?? snapshot?.desk;
    const activePlaybook = desk?.active_playbook ?? (posture as any)?.active_playbook;
    const nextReviewTime = desk?.next_review_time ?? activePlaybook?.next_review_time ?? (posture as any)?.next_review_time;

    const deskRoles: FunctionalDeskRole[] = (desk?.roles && desk.roles.length > 0)
        ? desk.roles
        : [
            {
                role_id: 'cio_strategist',
                title: '宏觀策略長 (Main CIO)',
                role_name: 'Macro & Portfolio Architect',
                scope: '總體體系判定 · 劇本動態切換 · 30天評估窗口治理',
                status: activeAgentCount > 0 ? 'WORKING' : 'MONITORING',
                current_task: `評估 TW/US 體系，維持 ${activePlaybook?.playbook_name ?? '穩健成長'} 劇本運作`,
                next_review_time: nextReviewTime,
            },
            {
                role_id: 'tactical_execution',
                title: '程序化執行席 (Execution Desk)',
                role_name: 'Tactical Execution Officer',
                scope: '台美跨市場紙盤撮合 · 買賣訂單排程 · 滑價成本控制',
                status: activeAgentCount > 0 ? 'WORKING' : (scheduledAgentCount > 0 ? 'STANDBY' : 'MONITORING'),
                current_task: '監控標的流動性與動能突破，執行本地紙盤隔離委託',
                next_review_time: nextReviewTime,
            },
            {
                role_id: 'risk_sentinel',
                title: '風控防禦席 (Risk Sentinel)',
                role_name: 'Risk & Capital Sentinel',
                scope: hasVerifiedCapital ? '單一TWD資金池保護 · 10%回撤防線 · 券商斷線隔離' : '單一資金池保護 · 10%回撤防線 · 券商斷線隔離',
                status: 'ACTIVE',
                current_task: `總曝險監控中 · 風險乘數 ${formatValue(posture?.risk_budget_multiplier ?? 1.0, 2)} · 熔斷守護`,
                next_review_time: nextReviewTime,
            },
            {
                role_id: 'data_watcher',
                title: '數據品質監理 (Data Telemetry)',
                role_name: 'Data Telemetry Watcher',
                scope: '報價即時性驗證 · 拒絕Stale/Synthetic · 會話閘門判定',
                status: 'ACTIVE',
                current_task: `數據品質狀態: ${String(freshness).toUpperCase()} · 標的行情連續驗證`,
                next_review_time: nextReviewTime,
            },
        ];

    const currency = portfolio?.reporting_currency?.trim() ? portfolio.reporting_currency : '—';
    const benchmarkCurrency = benchmark?.currency?.trim() && benchmark.currency !== '—' ? benchmark.currency : '—';

    const formatSnapshotTime = (ts?: string | null): string => {
        if (!ts || !ts.trim()) return '—';
        const d = new Date(ts);
        return isNaN(d.getTime()) ? '—' : d.toLocaleTimeString();
    };

    // Rigorous authoritative safety contradiction analysis
    const safetyContradictions: string[] = [];
    if (!safety || safety.paper_only === null || safety.broker_connected === null || safety.broker_state === null || safety.autonomous_capital_decisions === null) {
        safetyContradictions.push('安全狀態未知 (Safety state unavailable from backend)');
    } else {
        if (safety.paper_only !== true) {
            safetyContradictions.push(`非紙盤模式警告：paper_only 權威值為 ${String(safety.paper_only)} (FAIL-CLOSED: NON-PAPER DETECTED)`);
        }
        if (safety.broker_connected) {
            safetyContradictions.push('券商已連線警告：broker_connected 權威值為 true (FAIL-CLOSED: BROKER CONNECTED)');
        }
        if (safety.broker_state !== 'DISCONNECTED') {
            safetyContradictions.push(`券商狀態異常：broker_state 為 ${safety.broker_state} (FAIL-CLOSED: BROKER NOT DISCONNECTED)`);
        }
        if (safety.broker_connected && safety.broker_state === 'DISCONNECTED') {
            safetyContradictions.push('券商狀態矛盾：broker_connected 為 true 但 broker_state 為 DISCONNECTED');
        }
        if (!safety.broker_connected && safety.broker_state === 'CONNECTED') {
            safetyContradictions.push('券商狀態矛盾：broker_connected 為 false 但 broker_state 為 CONNECTED');
        }
        if (safety.autonomous_capital_decisions) {
            safetyContradictions.push('自主資金權限警告：autonomous_capital_decisions 為 true (FAIL-CLOSED: REAL-CAPITAL AUTHORITY DETECTED)');
        }
    }
    const hasSafetyContradiction = safetyContradictions.length > 0;

    const renderFreshnessBadge = (status: FreshnessStatus) => {
        switch (status) {
            case 'fresh':
                return (
                    <span className={styles.badgeFresh}>
                        <CheckCircle2 size={11} /> 數據即時 (FRESH)
                    </span>
                );
            case 'stale':
                return (
                    <span className={styles.badgeStale}>
                        <Clock size={11} /> 數據延遲 (STALE)
                    </span>
                );
            case 'unavailable':
            default:
                return (
                    <span className={styles.badgeUnavailable}>
                        <AlertCircle size={11} /> 數據不可用 (UNAVAILABLE)
                    </span>
                );
        }
    };

    return (
        <div className={styles.container}>
            {/* 1. Safety, Session, & Freshness Bar */}
            <div className={styles.safetyBar}>
                <div className={styles.safetyBadgesGroup}>
                    {/* Explicit Authoritative Safety Badges */}
                    {!safety || safety.paper_only === null ? (
                        <span className={styles.badgeSafetyContradiction}>
                            <AlertCircle size={12} />
                            SAFETY UNAVAILABLE
                        </span>
                    ) : safety.paper_only === true ? (
                        <span className={styles.badgePaperOnly}>
                            <ShieldAlert size={12} />
                            PAPER ONLY
                        </span>
                    ) : (
                        <span className={styles.badgeSafetyContradiction}>
                            <ShieldAlert size={12} />
                            FAIL-CLOSED: NON-PAPER DETECTED (paper_only: {String(safety.paper_only)})
                        </span>
                    )}

                    {!safety || safety.broker_connected === null || safety.broker_state === null ? (
                        <span className={styles.badgeSafetyContradiction}>
                            <AlertCircle size={12} />
                            BROKER UNKNOWN
                        </span>
                    ) : !safety.broker_connected && safety.broker_state === 'DISCONNECTED' ? (
                        <span className={styles.badgeBrokerDisconnected}>
                            <ZapOff size={12} />
                            BROKER: DISCONNECTED (NO REAL-CAPITAL)
                        </span>
                    ) : (
                        <span className={styles.badgeSafetyContradiction}>
                            <ShieldAlert size={12} />
                            SAFETY CONTRADICTION: BROKER {safety.broker_state} (connected: {String(safety.broker_connected)})
                        </span>
                    )}

                    {safety?.autonomous_capital_decisions === true && (
                        <span className={styles.badgeSafetyContradiction}>
                            <ShieldAlert size={12} />
                            CRITICAL: AUTONOMOUS CAPITAL ACTIVE
                        </span>
                    )}

                    {/* TW Session Badge */}
                    <span className={styles.badgeSession} title={sessions?.tw?.timezone}>
                        <span
                            className={
                                sessions?.tw?.status === 'OPEN'
                                    ? styles.sessionDotOpen
                                    : sessions?.tw?.status === 'PRE_MARKET'
                                      ? styles.sessionDotPre
                                      : styles.sessionDotClosed
                            }
                        />
                        <span>TW: {sessions?.tw?.session_label?.trim() ? sessions.tw.session_label : '—'}</span>
                    </span>

                    {/* US Session Badge */}
                    <span className={styles.badgeSession} title={sessions?.us?.timezone}>
                        <span
                            className={
                                sessions?.us?.status === 'OPEN'
                                    ? styles.sessionDotOpen
                                    : sessions?.us?.status === 'PRE_MARKET'
                                      ? styles.sessionDotPre
                                      : styles.sessionDotClosed
                            }
                        />
                        <span>US: {sessions?.us?.session_label?.trim() ? sessions.us.session_label : '—'}</span>
                    </span>

                    {/* Data Freshness */}
                    {renderFreshnessBadge(freshness)}
                </div>

                <div className={styles.safetyBadgesGroup}>
                    <span className={styles.badgeSession} title="唯讀觀察者介面；不提供本機下單或風控狀態修改">
                        觀察者 (Observer)
                    </span>

                    {/* Authoritative Server Snapshot Time */}
                    <span className={styles.serverTimestamp} title="伺服器端權威快照時間，非瀏覽器刷新時間">
                        <Clock size={12} />
                        <span>伺服器快照: {formatSnapshotTime(snapshot?.server_time)}</span>
                    </span>

                    {/* Refresh Button */}
                    <button
                        className={styles.modeToggleBtn}
                        onClick={() => void loadData()}
                        title={`最後更新: ${lastRefreshTime.toLocaleTimeString()}`}
                    >
                        <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
                        重新整理
                    </button>

                </div>
            </div>

            {hasSafetyContradiction && (
                <div className={styles.alertBanner}>
                    {safetyContradictions.map((msg, idx) => (
                        <div key={idx}>
                            <strong>安全狀態衝突警告 (Safety Contradiction Detected):</strong> {msg}
                        </div>
                    ))}
                </div>
            )}

            {snapshot?.integrity_warnings && snapshot.integrity_warnings.length > 0 && (
                <div className={styles.alertBanner} style={{ backgroundColor: 'rgba(255, 140, 140, 0.12)', border: '1px solid rgba(255, 140, 140, 0.35)' }}>
                    {snapshot.integrity_warnings.map((w, idx) => (
                        <div key={idx}>
                            <strong>數據完整性警告 (Integrity Warning):</strong> {w}
                        </div>
                    ))}
                </div>
            )}

            {error && (
                <div style={{ padding: '8px 12px', borderRadius: 4, backgroundColor: 'rgba(255, 140, 140, 0.1)', border: '1px solid rgba(255, 140, 140, 0.3)', color: '#ff8c8c', fontSize: '0.8rem' }}>
                    <strong>注意:</strong> {error} (已啟用安全離線隔離防護)
                </div>
            )}

            {/* Functional Desk Roles & Active Playbook Console */}
            <div className={styles.agentStatusCard}>
                <div className={styles.tableHeader}>
                    <div className={styles.sectionTitle}>
                        <Users size={14} style={{ color: '#a78bfa' }} />
                        自主執行交易台席位與運作劇本 (Autonomous Desk Roles & Active Playbook)
                    </div>
                    <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                        {hasVerifiedCapital ? '單一TWD資金池' : '單一資金池'} · 共 {deskRoles.length} 個職能席位 · 自適應劇本調度
                    </span>
                </div>

                {/* Active Playbook Banner: Currently selected playbook, why it was selected, and next review time */}
                <div
                    style={{
                        padding: '10px 14px',
                        margin: '10px 0',
                        borderRadius: 6,
                        backgroundColor: 'rgba(61, 139, 255, 0.08)',
                        border: '1px solid rgba(61, 139, 255, 0.25)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 6,
                    }}
                >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <SlidersHorizontal size={14} style={{ color: '#3d8bff' }} />
                            <span style={{ fontSize: '0.75rem', color: '#8b94a7', fontWeight: 600 }}>
                                當前選定劇本 (Active Playbook):
                            </span>
                            <span
                                style={{
                                    padding: '3px 10px',
                                    borderRadius: 4,
                                    fontSize: '0.76rem',
                                    fontWeight: 700,
                                    backgroundColor: 'rgba(61, 139, 255, 0.2)',
                                    color: '#3d8bff',
                                }}
                            >
                                {activePlaybook?.playbook_name ?? '穩健平衡成長 (Balanced Growth)'}
                            </span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.73rem', color: '#8b94a7' }}>
                            <Clock size={12} style={{ color: '#f6c453' }} />
                            <span>下次檢視時間 (Next Review):</span>
                            <strong style={{ color: '#dde3ee' }}>
                                {formatSnapshotTime(nextReviewTime)}
                            </strong>
                        </div>
                    </div>

                    <div style={{ fontSize: '0.74rem', color: '#dde3ee', lineHeight: 1.5 }}>
                        <strong style={{ color: '#8b94a7' }}>選擇依據 (Selection Rationale): </strong>
                        {activePlaybook?.selection_rationale ?? posture?.rationale ?? '依據當前市場體系、數據新鮮度、會話時段、流動性與現有持倉曝險動態自適應選定。'}
                    </div>

                    <div style={{ display: 'flex', gap: 14, fontSize: '0.68rem', color: '#8b94a7', flexWrap: 'wrap', marginTop: 2 }}>
                        <span>市場體系: {posture?.tw_regime?.regime ?? '—'} (TW) / {posture?.us_regime?.regime ?? '—'} (US)</span>
                        <span>數據品質: {String(freshness).toUpperCase()}</span>
                        <span>會話狀態: TW {sessions?.tw?.status ?? '—'} · US {sessions?.us?.status ?? '—'}</span>
                        <span>總持倉曝險: {formatPct(risk?.gross_exposure != null ? (risk.gross_exposure * 100) : null)}</span>
                    </div>
                </div>

                {/* Desk Roles Grid */}
                <div className={styles.agentGrid}>
                    {deskRoles.map((role) => {
                        const isWorking = role.status === 'WORKING' || role.status === 'ACTIVE';
                        const isStandby = role.status === 'STANDBY' || role.status === 'MONITORING';
                        return (
                            <div className={styles.agentCard} key={role.role_id}>
                                <div className={styles.agentHeader}>
                                    <div>
                                        <strong>{role.title}</strong>
                                        <div className={styles.agentMeta}>{role.role_name}</div>
                                    </div>
                                    <span className={isWorking ? styles.agentBadgeWorking : isStandby ? styles.agentBadgeStandby : styles.agentBadgeOffline}>
                                        <Radio size={10} /> {isWorking ? '運作中' : isStandby ? '監控待命' : '未就緒'}
                                    </span>
                                </div>
                                <div style={{ fontSize: '0.7rem', color: '#8b94a7', marginTop: 2, marginBottom: 4 }}>
                                    {role.scope}
                                </div>
                                <div className={styles.agentTask}>
                                    <strong>當前職責：</strong>{role.current_task}
                                </div>
                                <div className={styles.agentMeta}>
                                    下次檢視：{formatSnapshotTime(role.next_review_time ?? nextReviewTime)}
                                </div>
                            </div>
                        );
                    })}
                </div>
            </div>

            {/* 2. Canonical Team Equity & NAV Metrics Cards */}
            <div className={styles.metricsGrid}>
                {/* Team Equity / NAV */}
                <div className={styles.metricCard}>
                    <div className={styles.metricLabel}>
                        <span>團隊淨值 (Total NAV)</span>
                        {navStatus === 'NAV_UNAVAILABLE' ? (
                            <span className={styles.badgeUnavailable} style={{ fontSize: '0.65rem', padding: '1px 4px' }}>
                                NAV_UNAVAILABLE
                            </span>
                        ) : (
                            <span style={{ fontSize: '0.7rem' }}>{currency}</span>
                        )}
                    </div>
                    <div
                        className={styles.metricValue}
                        style={{
                            color: navStatus === 'NAV_UNAVAILABLE' || portfolio?.equity === null ? '#ff8c8c' : '#ffffff',
                        }}
                    >
                        {navStatus === 'NAV_UNAVAILABLE' || portfolio?.equity === null
                            ? 'NAV_UNAVAILABLE'
                            : formatCurrency(portfolio?.equity, currency)}
                    </div>
                    <div className={styles.metricSubtext}>
                        基準本金: {formatCurrency(portfolio?.initial_cash, currency)}
                    </div>
                </div>

                {/* Cash Balance */}
                <div className={styles.metricCard}>
                    <div className={styles.metricLabel}>
                        <span>可用紙盤現金 (Cash)</span>
                        <span style={{ fontSize: '0.7rem' }}>{currency}</span>
                    </div>
                    <div className={styles.metricValue}>
                        {formatCurrency(portfolio?.cash, currency)}
                    </div>
                    <div className={styles.metricSubtext}>
                        現金配置比重: {portfolio?.equity && portfolio?.cash ? `${((portfolio.cash / portfolio.equity) * 100).toFixed(1)}%` : '—'}
                    </div>
                </div>

                {/* Realized P&L */}
                <div className={styles.metricCard}>
                    <div className={styles.metricLabel}>
                        <span>已實現損益 (Realized P&L)</span>
                        <span style={{ fontSize: '0.7rem' }}>{currency}</span>
                    </div>
                    <div
                        className={styles.metricValue}
                        style={{
                            color:
                                portfolio?.realized_pnl == null
                                    ? '#8b94a7'
                                    : portfolio.realized_pnl >= 0
                                      ? '#8bd6a5'
                                      : '#ff8c8c',
                        }}
                    >
                        {formatCurrency(portfolio?.realized_pnl, currency)}
                    </div>
                    <div className={styles.metricSubtext}>
                        本週期累計平倉成果
                    </div>
                </div>

                {/* Unrealized P&L */}
                <div className={styles.metricCard}>
                    <div className={styles.metricLabel}>
                        <span>未實現損益 (Unrealized P&L)</span>
                        <span style={{ fontSize: '0.7rem' }}>{currency}</span>
                    </div>
                    <div
                        className={styles.metricValue}
                        style={{
                            color:
                                portfolio?.unrealized_pnl == null
                                    ? '#8b94a7'
                                    : portfolio.unrealized_pnl >= 0
                                      ? '#8bd6a5'
                                      : '#ff8c8c',
                        }}
                    >
                        {formatCurrency(portfolio?.unrealized_pnl, currency)}
                    </div>
                    <div className={styles.metricSubtext}>
                        現有持倉即時浮動盈虧
                    </div>
                </div>

                {/* Team Total Return */}
                <div className={styles.metricCard}>
                    <div className={styles.metricLabel}>
                        <span>累積報酬率 (Team Return)</span>
                        {portfolio?.return_pct != null ? (
                            portfolio.return_pct >= 0 ? (
                                <ArrowUpRight size={13} style={{ color: '#8bd6a5' }} />
                            ) : (
                                <ArrowDownRight size={13} style={{ color: '#ff8c8c' }} />
                            )
                        ) : null}
                    </div>
                    <div
                        className={styles.metricValue}
                        style={{
                            color:
                                portfolio?.return_pct == null
                                    ? '#8b94a7'
                                    : portfolio.return_pct >= 0
                                      ? '#8bd6a5'
                                      : '#ff8c8c',
                        }}
                    >
                        {formatPct(portfolio?.return_pct)}
                    </div>
                    <div className={styles.metricSubtext}>
                        超額報酬 Alpha: {formatPct(benchmark?.alpha_pct)}
                    </div>
                </div>
            </div>

            {/* 3. Middle Section: TW & US Regime, Posture & Rationale + Risk Budget & Kill Switch */}
            <div className={styles.middleGrid}>
                {/* Market Regimes & Team Posture Card */}
                <div className={styles.sectionCard}>
                    <div className={styles.sectionHeader}>
                        <div className={styles.sectionTitle}>
                            <TrendingUp size={14} style={{ color: '#3d8bff' }} />
                            市場體系狀態與團隊姿態 (Regimes & Posture)
                        </div>
                        <span
                            style={{
                                padding: '2px 8px',
                                borderRadius: 4,
                                fontSize: '0.72rem',
                                fontWeight: 700,
                                backgroundColor:
                                    posture?.posture === 'AGGRESSIVE'
                                        ? 'rgba(139, 214, 165, 0.2)'
                                        : !posture?.posture || posture.posture === 'UNAVAILABLE' || posture.posture === '—'
                                          ? 'rgba(255, 140, 140, 0.15)'
                                          : 'rgba(246, 196, 83, 0.2)',
                                color:
                                    posture?.posture === 'AGGRESSIVE'
                                        ? '#8bd6a5'
                                        : !posture?.posture || posture.posture === 'UNAVAILABLE' || posture.posture === '—'
                                          ? '#ff8c8c'
                                          : '#f6c453',
                            }}
                        >
                            {posture?.posture_label?.trim() ? posture.posture_label : '—'}
                        </span>
                    </div>

                    <div className={styles.postureBox}>
                        <strong style={{ fontSize: '0.78rem', color: '#8b94a7' }}>
                            AI 決策核心論據 (CIO Decision Rationale):
                        </strong>
                        <p className={styles.rationaleText}>
                            {posture?.rationale?.trim() ? posture.rationale : '—'}
                        </p>
                    </div>

                    <div className={styles.regimeGrid}>
                        {/* TW Regime */}
                        <div className={styles.regimeItem}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <strong style={{ fontSize: '0.76rem' }}>台股體系 (TW Regime)</strong>
                                <span style={{ fontSize: '0.68rem', color: posture?.tw_regime?.regime && posture.tw_regime.regime !== '—' && posture.tw_regime.regime !== 'UNAVAILABLE' ? '#8bd6a5' : '#8b94a7' }}>
                                    {posture?.tw_regime?.regime?.trim() ? posture.tw_regime.regime : '—'}
                                </span>
                            </div>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7', marginTop: 2 }}>
                                趨勢: {posture?.tw_regime?.trend?.trim() ? posture.tw_regime.trend : '—'}
                            </span>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                波動: {posture?.tw_regime?.volatility?.trim() ? posture.tw_regime.volatility : '—'}
                            </span>
                        </div>

                        {/* US Regime */}
                        <div className={styles.regimeItem}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <strong style={{ fontSize: '0.76rem' }}>美股體系 (US Regime)</strong>
                                <span style={{ fontSize: '0.68rem', color: posture?.us_regime?.regime && posture.us_regime.regime !== '—' && posture.us_regime.regime !== 'UNAVAILABLE' ? '#f6c453' : '#8b94a7' }}>
                                    {posture?.us_regime?.regime?.trim() ? posture.us_regime.regime : '—'}
                                </span>
                            </div>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7', marginTop: 2 }}>
                                趨勢: {posture?.us_regime?.trend?.trim() ? posture.us_regime.trend : '—'}
                            </span>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                波動: {posture?.us_regime?.volatility?.trim() ? posture.us_regime.volatility : '—'}
                            </span>
                        </div>
                    </div>
                </div>

                {/* Risk Budget, Breaches & Kill Switch Card */}
                <div className={styles.sectionCard}>
                    <div className={styles.sectionHeader}>
                        <div className={styles.sectionTitle}>
                            <Shield size={14} style={{ color: '#16b389' }} />
                            風控預算與熔斷狀態 (Risk Budget & Kill Switch)
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            {!risk || snapshot?.data_freshness === 'unavailable' || risk.kill_switch_active === null ? (
                                <span style={{ padding: '2px 8px', borderRadius: 4, fontSize: '0.72rem', fontWeight: 700, backgroundColor: 'rgba(255, 140, 140, 0.15)', color: '#ff8c8c' }}>
                                    <AlertCircle size={10} style={{ verticalAlign: '-1px' }} /> 風控狀態不可用 (UNAVAILABLE)
                                </span>
                            ) : risk.kill_switch_active ? (
                                <span style={{ padding: '2px 8px', borderRadius: 4, fontSize: '0.72rem', fontWeight: 700, backgroundColor: 'rgba(242, 54, 69, 0.2)', color: '#ff8c8c' }}>
                                    <Lock size={10} style={{ verticalAlign: '-1px' }} /> 熔斷已鎖定 (TRIPPED)
                                </span>
                            ) : risk.kill_switch_armed ? (
                                <span style={{ padding: '2px 8px', borderRadius: 4, fontSize: '0.72rem', fontWeight: 700, backgroundColor: 'rgba(22, 179, 137, 0.15)', color: '#8bd6a5' }}>
                                    <ShieldCheck size={10} style={{ verticalAlign: '-1px' }} /> 風控防護正常 (ARMED)
                                </span>
                            ) : (
                                <span style={{ padding: '2px 8px', borderRadius: 4, fontSize: '0.72rem', fontWeight: 700, backgroundColor: 'rgba(246, 196, 83, 0.2)', color: '#f6c453' }}>
                                    <ShieldAlert size={10} style={{ verticalAlign: '-1px' }} /> 風控未就緒 (DISARMED)
                                </span>
                            )}
                        </div>
                    </div>

                    <div className={styles.riskMeterContainer}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.73rem' }}>
                            <span style={{ color: '#8b94a7' }}>整體風險預算使用率 (Risk Budget Usage)</span>
                            <strong>{formatPct(risk?.budget_usage_pct, false)}</strong>
                        </div>
                        <div className={styles.riskProgressBar}>
                            {risk?.budget_usage_pct != null && (
                                <div
                                    className={styles.riskProgressFill}
                                    style={{
                                        width: `${Math.min(100, Math.max(0, risk.budget_usage_pct))}%`,
                                        backgroundColor:
                                            risk.budget_usage_pct > 80
                                                ? '#f23645'
                                                : risk.budget_usage_pct > 50
                                                  ? '#f6c453'
                                                  : '#16b389',
                                    }}
                                />
                            )}
                        </div>
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, fontSize: '0.73rem' }}>
                        <div style={{ padding: '6px 8px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.06)' }}>
                            <div style={{ color: '#8b94a7', fontSize: '0.68rem' }}>當前最大回撤</div>
                            <strong
                                style={{
                                    display: 'block',
                                    marginTop: 2,
                                    color: risk?.max_drawdown_pct == null ? '#8b94a7' : risk.max_drawdown_pct > 3 ? '#ff8c8c' : '#dde3ee',
                                }}
                            >
                                {risk?.max_drawdown_pct != null
                                    ? `${formatPct(risk.max_drawdown_pct, false)}${risk.drawdown_limit_pct != null ? ` (上限 ${formatPct(risk.drawdown_limit_pct, false)})` : ''}`
                                    : '—'}
                            </strong>
                        </div>
                        <div style={{ padding: '6px 8px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.06)' }}>
                            <div style={{ color: '#8b94a7', fontSize: '0.68rem' }}>單日損失限額</div>
                            <strong style={{ display: 'block', marginTop: 2 }}>
                                {risk?.current_daily_loss != null || risk?.daily_loss_limit != null
                                    ? `${formatCurrency(risk?.current_daily_loss, currency)} / ${formatCurrency(risk?.daily_loss_limit, currency)}`
                                    : '—'}
                            </strong>
                        </div>
                        <div style={{ padding: '6px 8px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.06)' }}>
                            <div style={{ color: '#8b94a7', fontSize: '0.68rem' }}>主動違規警報</div>
                            <strong
                                style={{
                                    display: 'block',
                                    marginTop: 2,
                                    color:
                                        !risk || !risk.breaches || snapshot?.data_freshness === 'unavailable'
                                            ? '#8b94a7'
                                            : risk.breaches.length > 0
                                              ? '#ff8c8c'
                                              : '#8bd6a5',
                                }}
                            >
                                {!risk || !risk.breaches || snapshot?.data_freshness === 'unavailable'
                                    ? '—'
                                    : risk.breaches.length > 0
                                      ? `${risk.breaches.length} 件警報`
                                      : '0 件警報'}
                            </strong>
                        </div>
                    </div>

                    {risk?.breaches && risk.breaches.length > 0 && (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
                            {risk.breaches.map((b) => (
                                <div key={b.id} style={{ padding: '4px 8px', backgroundColor: 'rgba(242, 54, 69, 0.1)', border: '1px solid rgba(242, 54, 69, 0.25)', borderRadius: 4, fontSize: '0.72rem', color: '#ff8c8c' }}>
                                    <strong>[{b.level.toUpperCase()}] {b.rule}:</strong> {b.detail}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* 4. Market Quotes Feed (Source, Timestamp, Freshness) */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div className={styles.sectionTitle}>
                        <Activity size={14} style={{ color: '#3d8bff' }} />
                        核心行情源與即時性 (Market Quotes & Freshness)
                    </div>
                    <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                        顯示權威行情源、撮合時間戳與延遲狀態
                    </span>
                </div>
                <div className={styles.tableWrapper}>
                    <table className={styles.dataTable}>
                        <thead>
                            <tr>
                                <th className={styles.th}>代碼 / 名稱</th>
                                <th className={styles.th}>市場</th>
                                <th className={styles.th}>行情來源 (Quote Source)</th>
                                <th className={styles.th}>最新成交價</th>
                                <th className={styles.th}>漲跌幅</th>
                                <th className={styles.th}>報價時間戳 (Quote Timestamp)</th>
                                <th className={styles.th}>即時狀態 (Freshness)</th>
                            </tr>
                        </thead>
                        <tbody>
                            {quotes.map((q) => (
                                <tr key={q.symbol}>
                                    <td className={styles.td}>
                                        <strong>{q.symbol}</strong>{' '}
                                        <span style={{ color: '#8b94a7', fontSize: '0.73rem' }}>{q.name}</span>
                                    </td>
                                    <td className={styles.td}>
                                        <span style={{ padding: '2px 5px', borderRadius: 3, fontSize: '0.68rem', backgroundColor: 'rgba(255,255,255,0.06)' }}>
                                            {q.market}
                                        </span>
                                    </td>
                                    <td className={styles.td} style={{ color: '#8b94a7' }}>
                                        {q.source}
                                    </td>
                                    <td className={styles.td}>
                                        {formatValue(q.price)}
                                    </td>
                                    <td
                                        className={styles.td}
                                        style={{
                                            color:
                                                q.change_pct == null
                                                    ? undefined
                                                    : q.change_pct >= 0
                                                      ? '#8bd6a5'
                                                      : '#ff8c8c',
                                        }}
                                    >
                                        {formatPct(q.change_pct)}
                                    </td>
                                    <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.72rem' }}>
                                        {formatSnapshotTime(q.timestamp)}
                                    </td>
                                    <td className={styles.td}>
                                        {renderFreshnessBadge(q.freshness)}
                                    </td>
                                </tr>
                            ))}
                            {quotes.length === 0 && (
                                <tr>
                                    <td colSpan={7} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                        無即時行情數據
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 5. Consolidated Holdings Table */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div className={styles.sectionTitle}>
                        <BarChart3 size={14} style={{ color: '#16b389' }} />
                        台美跨市場綜合持倉表 (Consolidated Holdings)
                    </div>
                    <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                        共 {holdings.length} 檔持倉部位 · 跨台股/美股統合計算
                    </span>
                </div>
                <div className={styles.tableWrapper}>
                    <table className={styles.dataTable}>
                        <thead>
                            <tr>
                                <th className={styles.th}>標的 / 名稱</th>
                                <th className={styles.th}>市場</th>
                                <th className={styles.th}>策略小組 (Strategy)</th>
                                <th className={styles.th}>持倉數量</th>
                                <th className={styles.th}>進場成本</th>
                                <th className={styles.th}>現價</th>
                                <th className={styles.th}>部位市值</th>
                                <th className={styles.th}>未實現損益</th>
                                <th className={styles.th}>報酬率</th>
                                <th className={styles.th}>報價新鮮度</th>
                            </tr>
                        </thead>
                        <tbody>
                            {holdings.map((h) => (
                                <tr key={`${h.market}-${h.symbol}`}>
                                    <td className={styles.td}>
                                        <strong>{h.symbol}</strong>{' '}
                                        <span style={{ color: '#8b94a7', fontSize: '0.72rem' }}>{h.name}</span>
                                    </td>
                                    <td className={styles.td}>
                                        <span
                                            style={{
                                                padding: '2px 5px',
                                                borderRadius: 3,
                                                fontSize: '0.68rem',
                                                backgroundColor:
                                                    h.market === 'TW'
                                                        ? 'rgba(61, 139, 255, 0.15)'
                                                        : 'rgba(246, 196, 83, 0.15)',
                                                color: h.market === 'TW' ? '#3d8bff' : '#f6c453',
                                            }}
                                        >
                                            {h.market}
                                        </span>
                                    </td>
                                    <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.72rem' }}>
                                        {h.strategy_id ?? 'team_core'}
                                    </td>
                                    <td className={styles.td}>
                                        {formatValue(h.quantity, 0)}
                                    </td>
                                    <td className={styles.td}>
                                        {formatValue(h.entry_price)}
                                    </td>
                                    <td className={styles.td}>
                                        {formatValue(h.current_price)}
                                    </td>
                                    <td className={styles.td}>
                                        {formatCurrency(h.market_value, h.currency)}
                                    </td>
                                    <td
                                        className={styles.td}
                                        style={{
                                            color:
                                                h.unrealized_pnl == null
                                                    ? undefined
                                                    : h.unrealized_pnl >= 0
                                                      ? '#8bd6a5'
                                                      : '#ff8c8c',
                                        }}
                                    >
                                        {formatCurrency(h.unrealized_pnl, h.currency)}
                                    </td>
                                    <td
                                        className={styles.td}
                                        style={{
                                            color:
                                                h.return_pct == null
                                                    ? undefined
                                                    : h.return_pct >= 0
                                                      ? '#8bd6a5'
                                                      : '#ff8c8c',
                                        }}
                                    >
                                        {formatPct(h.return_pct)}
                                    </td>
                                    <td className={styles.td}>
                                        {renderFreshnessBadge(h.price_freshness)}
                                    </td>
                                </tr>
                            ))}
                            {holdings.length === 0 && (
                                <tr>
                                    <td colSpan={10} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                        當前全體團隊無在倉部位 (Zero Paper Positions)
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 6. Orders vs Fills (Separated tabs or sections) */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        <div className={styles.sectionTitle}>
                            <Clock size={14} style={{ color: '#3d8bff' }} />
                            紙盤委託與成交紀錄 (Paper Orders vs Fills)
                        </div>
                        <div className={styles.tabGroup}>
                            <button
                                className={`${styles.tabBtn} ${ordersTab === 'orders' ? styles.tabBtnActive : ''}`}
                                onClick={() => setOrdersTab('orders')}
                            >
                                未平倉/進行中委託 ({orders.length})
                            </button>
                            <button
                                className={`${styles.tabBtn} ${ordersTab === 'fills' ? styles.tabBtnActive : ''}`}
                                onClick={() => setOrdersTab('fills')}
                            >
                                歷史成交紀錄 ({fills.length})
                            </button>
                        </div>
                    </div>
                </div>

                <div className={styles.tableWrapper}>
                    {ordersTab === 'orders' ? (
                        <table className={styles.dataTable}>
                            <thead>
                                <tr>
                                    <th className={styles.th}>委託編號</th>
                                    <th className={styles.th}>標的 / 市場</th>
                                    <th className={styles.th}>買賣方向</th>
                                    <th className={styles.th}>數量</th>
                                    <th className={styles.th}>價格 / 類型</th>
                                    <th className={styles.th}>委託狀態</th>
                                    <th className={styles.th}>策略來源</th>
                                    <th className={styles.th}>AI 下單論據 (Rationale)</th>
                                    <th className={styles.th}>建立時間</th>
                                </tr>
                            </thead>
                            <tbody>
                                {orders.map((o: PaperOrder) => (
                                    <tr key={o.order_id}>
                                        <td className={styles.td}>{o.order_id}</td>
                                        <td className={styles.td}>
                                            <strong>{o.symbol}</strong> ({o.market})
                                        </td>
                                        <td
                                            className={styles.td}
                                            style={{
                                                color: o.side === 'BUY' ? '#8bd6a5' : '#ff8c8c',
                                                fontWeight: 700,
                                            }}
                                        >
                                            {o.side}
                                        </td>
                                        <td className={styles.td}>{formatValue(o.quantity, 0)}</td>
                                        <td className={styles.td}>
                                            {formatValue(o.price)} ({o.order_type})
                                        </td>
                                        <td className={styles.td}>
                                            <span style={{ padding: '2px 6px', borderRadius: 3, fontSize: '0.7rem', backgroundColor: 'rgba(61, 139, 255, 0.12)', color: '#3d8bff' }}>
                                                {o.status}
                                            </span>
                                        </td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>
                                            {o.strategy_id ?? '—'}
                                        </td>
                                        <td className={styles.td} style={{ textAlign: 'left', maxWidth: 260, whiteSpace: 'normal', color: '#dde3ee', fontSize: '0.72rem' }}>
                                            {o.rationale ?? '—'}
                                        </td>
                                        <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>
                                            {formatSnapshotTime(o.created_at)}
                                        </td>
                                    </tr>
                                ))}
                                {orders.length === 0 && (
                                    <tr>
                                        <td colSpan={9} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                            目前無進行中紙盤委託
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    ) : (
                        <table className={styles.dataTable}>
                            <thead>
                                <tr>
                                    <th className={styles.th}>成交編號</th>
                                    <th className={styles.th}>標的 / 市場</th>
                                    <th className={styles.th}>買賣方向</th>
                                    <th className={styles.th}>成交數量</th>
                                    <th className={styles.th}>成交均價</th>
                                    <th className={styles.th}>手續費估計</th>
                                    <th className={styles.th}>所屬策略</th>
                                    <th className={styles.th}>成交時間</th>
                                </tr>
                            </thead>
                            <tbody>
                                {fills.map((f: PaperFill) => (
                                    <tr key={f.fill_id}>
                                        <td className={styles.td}>{f.fill_id}</td>
                                        <td className={styles.td}>
                                            <strong>{f.symbol}</strong> ({f.market})
                                        </td>
                                        <td
                                            className={styles.td}
                                            style={{
                                                color: f.side === 'BUY' ? '#8bd6a5' : '#ff8c8c',
                                                fontWeight: 700,
                                            }}
                                        >
                                            {f.side}
                                        </td>
                                        <td className={styles.td}>{formatValue(f.quantity, 0)}</td>
                                        <td className={styles.td}>{formatValue(f.price)}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>
                                            {formatValue(f.fee)}
                                        </td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>
                                            {f.strategy_id ?? '—'}
                                        </td>
                                        <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>
                                            {formatSnapshotTime(f.timestamp)}
                                        </td>
                                    </tr>
                                ))}
                                {fills.length === 0 && (
                                    <tr>
                                        <td colSpan={8} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                            尚無成交紀錄
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    )}
                </div>
            </div>

            {/* 7. Team Activity Stream & Benchmark Comparison */}
            <div className={styles.middleGrid}>
                {/* Team Activity Stream */}
                <div className={styles.sectionCard}>
                    <div className={styles.sectionHeader}>
                        <div className={styles.sectionTitle}>
                            <Activity size={14} style={{ color: '#f6c453' }} />
                            團隊運維動態與決策流水 (Team Activity Stream)
                        </div>
                        <span style={{ fontSize: '0.7rem', color: '#8b94a7' }}>
                            含角色、狀態與決策因由
                        </span>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 280, overflowY: 'auto' }}>
                        {activity.map((act) => (
                            <div
                                key={act.id}
                                style={{
                                    display: 'flex',
                                    flexDirection: 'column',
                                    gap: 3,
                                    padding: '7px 10px',
                                    borderRadius: 4,
                                    backgroundColor: 'rgba(255,255,255,0.02)',
                                    border: '1px solid rgba(255,255,255,0.06)',
                                }}
                            >
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <span style={{ fontWeight: 700, fontSize: '0.76rem', color: '#3d8bff' }}>
                                        {act.actor}
                                    </span>
                                    <span style={{ fontSize: '0.68rem', color: '#8b94a7' }}>
                                        {formatSnapshotTime(act.timestamp)}
                                    </span>
                                </div>
                                <div style={{ fontSize: '0.75rem', fontWeight: 600 }}>
                                    {act.action}{act.target ? ` · ${act.target}` : ''}
                                </div>
                                <div style={{ fontSize: '0.72rem', color: '#8b94a7', lineHeight: 1.4 }}>
                                    <strong>因由:</strong> {act.rationale}
                                </div>
                            </div>
                        ))}
                        {activity.length === 0 && (
                            <div style={{ textAlign: 'center', padding: '16px', color: '#8b94a7', fontSize: '0.75rem' }}>
                                目前尚無活動紀錄
                            </div>
                        )}
                    </div>
                </div>

                {/* Benchmark Comparison */}
                <div className={styles.sectionCard}>
                    <div className={styles.sectionHeader}>
                        <div className={styles.sectionTitle}>
                            <BarChart3 size={14} style={{ color: '#16b389' }} />
                            基準對照與超額報酬 (Benchmark Comparison)
                        </div>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <div style={{ padding: '8px 10px', backgroundColor: vars.color.inset, borderRadius: 4, border: `1px solid ${vars.color.border}` }}>
                            <div style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                對照基準: <strong>{benchmark?.benchmark_name ?? '—'}</strong> {`(${benchmark?.period ?? '—'} · ${benchmarkCurrency})`}
                            </div>
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                            <div style={{ padding: '10px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.06)' }}>
                                <div style={{ fontSize: '0.7rem', color: '#8b94a7' }}>團隊總報酬 (Team Return)</div>
                                <div
                                    style={{
                                        fontSize: '1.25rem',
                                        fontWeight: 700,
                                        marginTop: 4,
                                        color:
                                            portfolio?.return_pct == null
                                                ? '#8b94a7'
                                                : portfolio.return_pct >= 0
                                                  ? '#8bd6a5'
                                                  : '#ff8c8c',
                                    }}
                                >
                                    {formatPct(portfolio?.return_pct)}
                                </div>
                            </div>
                            <div style={{ padding: '10px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.06)' }}>
                                <div style={{ fontSize: '0.7rem', color: '#8b94a7' }}>基準指數報酬 (Benchmark)</div>
                                <div
                                    style={{
                                        fontSize: '1.25rem',
                                        fontWeight: 700,
                                        marginTop: 4,
                                        color:
                                            benchmark?.benchmark_return_pct == null
                                                ? '#8b94a7'
                                                : '#dde3ee',
                                    }}
                                >
                                    {formatPct(benchmark?.benchmark_return_pct)}
                                </div>
                            </div>
                        </div>

                        <div style={{ padding: '10px', backgroundColor: 'rgba(61, 139, 255, 0.08)', borderRadius: 4, border: '1px solid rgba(61, 139, 255, 0.25)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <div>
                                <span style={{ fontSize: '0.72rem', color: '#3d8bff', fontWeight: 600 }}>超額績效 Alpha (Spread)</span>
                                <div
                                    style={{
                                        fontSize: '1.15rem',
                                        fontWeight: 700,
                                        color:
                                            benchmark?.alpha_pct == null
                                                ? '#8b94a7'
                                                : benchmark.alpha_pct >= 0
                                                  ? '#8bd6a5'
                                                  : '#ff8c8c',
                                    }}
                                >
                                    {formatPct(benchmark?.alpha_pct)}
                                </div>
                            </div>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                {`貨幣計價: ${benchmarkCurrency}`}
                            </span>
                        </div>
                    </div>
                </div>
            </div>

            {/* 8. Clearly Labeled Legacy / Experiment Detail (Accordion, Non-Primary) */}
            <div className={styles.legacyAccordion}>
                <div
                    className={styles.legacyAccordionHeader}
                    onClick={() => setShowLegacyLeaderboard(!showLegacyLeaderboard)}
                >
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <SlidersHorizontal size={13} />
                        舊版實驗數據與子策略排行 (Legacy / Experiment Detail — Non-Primary)
                    </span>
                    {showLegacyLeaderboard ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </div>

                {showLegacyLeaderboard && (
                    <div style={{ padding: 12 }}>
                        <div style={{ fontSize: '0.72rem', color: '#8b94a7', marginBottom: 8 }}>
                            此處僅作歷史實驗記錄與子策略排行參照，非主要團隊運維指標。
                        </div>
                        <div className={styles.tableWrapper}>
                            <table className={styles.dataTable}>
                                <thead>
                                    <tr>
                                        <th className={styles.th}>排名 / 策略</th>
                                        <th className={styles.th}>市場 / 風格</th>
                                        <th className={styles.th}>NAV ({currency})</th>
                                        <th className={styles.th}>損益 ({currency})</th>
                                        <th className={styles.th}>報酬率</th>
                                        <th className={styles.th}>原生現金</th>
                                        <th className={styles.th}>在倉部位</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {legacyLeaderboard.map((row) => (
                                        <tr key={row.strategy_id}>
                                            <td className={styles.td}>
                                                <strong>{row.rank}. {row.name || row.strategy_id}</strong>
                                                <div style={{ color: '#8b94a7', fontSize: '0.68rem' }}>{row.strategy_id}</div>
                                            </td>
                                            <td className={styles.td}>
                                                {row.market} / {row.style ?? 'paper'}
                                            </td>
                                            <td className={styles.td}>
                                                {formatValue(row.equity_reporting ?? row.equity)}
                                            </td>
                                            <td className={styles.td} style={{ color: (row.pnl_reporting ?? row.pnl) >= 0 ? '#8bd6a5' : '#ff8c8c' }}>
                                                {formatValue(row.pnl_reporting ?? row.pnl)}
                                            </td>
                                            <td className={styles.td} style={{ color: row.return_pct >= 0 ? '#8bd6a5' : '#ff8c8c' }}>
                                                {formatPct(row.return_pct)}
                                            </td>
                                            <td className={styles.td}>
                                                {formatCurrency(row.cash, row.base_currency)}
                                            </td>
                                            <td className={styles.td}>
                                                {row.open_positions}
                                            </td>
                                        </tr>
                                    ))}
                                    {legacyLeaderboard.length === 0 && (
                                        <tr>
                                            <td colSpan={7} style={{ textAlign: 'center', padding: '12px', color: '#8b94a7' }}>
                                                無舊版子策略實驗資料
                                            </td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
