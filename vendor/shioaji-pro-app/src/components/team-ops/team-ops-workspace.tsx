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
import { Fragment, useCallback, useEffect, useState } from 'react';
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
    RoundTrip,
    TeamOpsSnapshot,
} from '../../lib/types/team-ops';
import { vars } from '../../theme.css';
import * as styles from './team-ops-workspace.css';

const NOTICE_TEXT: Array<[string, string]> = [
    ['LIVE_PAPER_RUNTIME_READ_ONLY', '資料來源是紙盤執行紀錄檔的唯讀快照，不是券商帳戶或正式結算資料。'],
    ['Per source equity', '各帳本（台股、美股、波段）各自獨立計算「現金＋持倉市值」，同代碼不合併；台幣與美元沒有可驗證匯率前，不加總成總淨值。'],
    ['UNKNOWN_RUNTIME_KILL_SWITCH', '目前沒有執行器回報熔斷開關狀態，頁面以保守的「已鎖定」顯示，並非實際觸發。'],
    ['UNKNOWN_MARK_TIMESTAMPS', '帳戶持倉價是紀錄當下的標記值，沒有逐筆時間戳：時間以帳戶檔最後存檔時間代替，並標示為延遲，不稱即時。'],
    ['UNKNOWN_INITIAL_CAPITAL_FX_BENCHMARK', '執行器未記錄初始本金、匯率與基準：不做跨幣別總淨值；各帳本報酬率改以起始資金計算（波段為程式常數，台股／美股帳戶為由損益回推的估計值，已標示）。'],
    ['SWING_INDIVIDUAL_MARKS_UNAVAILABLE', '波段帳本沒有逐檔即時價；持倉旁的價格改用官方收盤價作參考，帳本總淨值沿用其自己記錄的收盤權益。'],
    ['SWING_PARAMS_VERSION_DIFFERS', '波段帳本進場時用的參數版本，與目前參數檔版本不同（參數之後又被更新過），屬正常的版本差異。'],
    ['HISTORY_BOUNDED', '委託、成交與動態流水只顯示最新 100 筆；完整交易（買賣配對）則由全部成交紀錄還原。'],
];

const zh = (v?: string | null): string => (!v || !v.trim() || v === 'UNKNOWN' || v === 'UNAVAILABLE' ? '未記錄' : v);

function isKnownNotice(raw: string): boolean {
    return NOTICE_TEXT.some(([key]) => raw.startsWith(key));
}

function describeNotice(raw: string): string {
    const hit = NOTICE_TEXT.find(([key]) => raw.startsWith(key));
    return hit ? hit[1] : raw;
}

const fmtTripTime = (ts?: string | null): string => {
    if (!ts) return '—';
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? ts : d.toLocaleString('zh-TW', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
};

const fmtHold = (a?: string | null, b?: string | null): string => {
    if (!a || !b) return '—';
    const mins = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 60000);
    if (!Number.isFinite(mins) || mins < 0) return '—';
    if (mins < 60) return `${mins} 分鐘`;
    if (mins < 1440) return `${(mins / 60).toFixed(1)} 小時`;
    return `${(mins / 1440).toFixed(1)} 天`;
};

export function TeamOpsWorkspace() {
    const [snapshot, setSnapshot] = useState<TeamOpsSnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastRefreshTime, setLastRefreshTime] = useState<Date>(new Date());
    const [ordersTab, setOrdersTab] = useState<'trips' | 'orders' | 'fills'>('trips');
    const [expandedTrip, setExpandedTrip] = useState<string | null>(null);
    const [tripLimit, setTripLimit] = useState(30);
    const [showNotices, setShowNotices] = useState(false);
    const [quoteSource, setQuoteSource] = useState('ALL');
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
    const quoteSources = Array.from(new Set(quotes.map((q) => q.source))).sort();
    const selectedQuoteSource = quoteSources.includes(quoteSource) ? quoteSource : 'ALL';
    const visibleQuotes = quotes.filter((q) => selectedQuoteSource === 'ALL' || q.source === selectedQuoteSource);
    const orders = snapshot?.orders ?? [];
    const fills = snapshot?.fills ?? [];
    const exitStats = (() => {
        const m = new Map<string, { strategy: string; currency: string; total: number; wins: number; pnl: number; by: Record<string, { n: number; pnl: number }> }>();
        for (const t of snapshot?.round_trips ?? []) {
            const key = `${t.strategy_id ?? '—'}|${t.currency}`;
            const r = m.get(key) ?? { strategy: t.strategy_id ?? '—', currency: t.currency, total: 0, wins: 0, pnl: 0, by: {} };
            const reason = (t.exit_reason ?? '').replace('exit:', '');
            r.total += 1; r.pnl += t.pnl; r.wins += t.pnl > 0 ? 1 : 0;
            r.by[reason] = { n: (r.by[reason]?.n ?? 0) + 1, pnl: (r.by[reason]?.pnl ?? 0) + t.pnl };
            m.set(key, r);
        }
        return [...m.values()].map(r => ({ ...r, winRate: (r.wins / r.total) * 100 })).sort((a, b) => b.total - a.total);
    })();
    const allNotices = snapshot?.integrity_warnings ?? [];
    const knownNotices = allNotices.filter(isKnownNotice);
    const unknownNotices = allNotices.filter(w => !isKnownNotice(w));
    const pendingOrders = orders.filter(o => o.status !== 'FILLED');
    const trips = [...(snapshot?.round_trips ?? [])].sort((a, b) => (b.sell_time ?? '').localeCompare(a.sell_time ?? ''));
    const tripSummary = ['TWD', 'USD'].map(currency => {
        const rows = trips.filter(t => t.currency === currency);
        return {
            currency,
            count: rows.length,
            pnl: rows.reduce((sum, t) => sum + t.pnl, 0),
            winRate: rows.length ? (rows.filter(t => t.pnl > 0).length / rows.length) * 100 : 0,
        };
    }).filter(s => s.count > 0);
    const activity = snapshot?.activity ?? [];
    const benchmark = snapshot?.benchmark;
    const legacyLeaderboard = snapshot?.legacy_leaderboard ?? [];
    const activeAgentCount = agents.filter(({ status }) => status.processing).length;
    const scheduledAgentCount = agents.filter(({ status }) => status.running).length;

    const desk = (posture as any)?.desk ?? snapshot?.desk;
    const activePlaybook = desk?.active_playbook ?? (posture as any)?.active_playbook;
    const nextReviewTime = desk?.next_review_time ?? activePlaybook?.next_review_time ?? (posture as any)?.next_review_time;

    const deskRoles: FunctionalDeskRole[] = desk?.roles ?? [];

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
                        <AlertCircle size={11} /> 無即時報價
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

            {unknownNotices.length > 0 && (
                <div className={styles.alertBanner}>
                    {unknownNotices.map((w, idx) => (
                        <div key={idx}><strong>數據完整性警告 (Integrity Warning):</strong> {w}</div>
                    ))}
                </div>
            )}
            {knownNotices.length > 0 && (
                <div style={{ padding: '6px 12px', borderRadius: 4, backgroundColor: 'rgba(139, 148, 167, 0.08)', border: '1px solid rgba(139, 148, 167, 0.25)', fontSize: '0.75rem', color: '#8b94a7' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', cursor: 'pointer' }} onClick={() => setShowNotices(v => !v)}>
                        <span>資料說明：本頁為唯讀紀錄回放（非即時券商資料），共 {knownNotices.length} 項已知限制，並非系統錯誤。</span>
                        <span>{showNotices ? '收合 ▲' : '展開 ▼'}</span>
                    </div>
                    {showNotices && (
                        <ul style={{ margin: '6px 0 0', paddingLeft: 18, lineHeight: 1.7 }}>
                            {knownNotices.map((w, idx) => (
                                <li key={idx}>{describeNotice(w)}</li>
                            ))}
                        </ul>
                    )}
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
                        <span>市場體系: {zh(posture?.tw_regime?.regime)} (TW) / {zh(posture?.us_regime?.regime)} (US)</span>
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
                                        <Radio size={10} /> {isWorking ? '運作中' : isStandby ? '監控待命' : '離線（顯示最後紀錄）'}
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

            {/* 2. Per-book results (each book is independent; never summed across currencies) */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div className={styles.sectionTitle}>
                        <TrendingUp size={14} style={{ color: '#3d8bff' }} />
                        各帳本成績 (Book Results)
                    </div>
                    {(navStatus === 'NAV_UNAVAILABLE' || portfolio?.equity == null) && (
                        <span className={styles.badgeUnavailable} style={{ fontSize: '0.65rem', padding: '1px 4px' }}>跨幣別總淨值 NAV_UNAVAILABLE（無匯率）</span>
                    )}
                    <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>三個帳本資金獨立、幣別不同，不加總；已實現損益由逐筆成交還原</span>
                </div>
                <div className={styles.tableWrapper}>
                    <table className={styles.dataTable}>
                        <thead>
                            <tr>
                                <th className={styles.th}>帳本</th>
                                <th className={styles.th}>淨值</th>
                                <th className={styles.th}>現金</th>
                                <th className={styles.th}>已實現</th>
                                <th className={styles.th}>未實現</th>
                                <th className={styles.th}>報酬率</th>
                                <th className={styles.th}>交易 / 勝率</th>
                                <th className={styles.th}>起始資金</th>
                            </tr>
                        </thead>
                        <tbody>
                            {Object.entries(portfolio?.sources ?? {}).map(([name, raw]) => {
                                const src = raw as Record<string, any>;
                                const cur = src.currency ?? '';
                                const pnlColor = (v: number | null | undefined) => (v == null ? '#8b94a7' : v >= 0 ? '#8bd6a5' : '#ff8c8c');
                                return (
                                    <tr key={name}>
                                        <td className={styles.td}><strong>{name}</strong> <span style={{ color: '#8b94a7' }}>({cur})</span></td>
                                        <td className={styles.td}>{formatCurrency(src.equity, cur)}</td>
                                        <td className={styles.td}>{formatCurrency(src.cash, cur)}</td>
                                        <td className={styles.td} style={{ color: pnlColor(src.realized_pnl_from_fills) }}>{src.trade_count ? formatCurrency(src.realized_pnl_from_fills, cur) : '尚無平倉'}</td>
                                        <td className={styles.td} style={{ color: pnlColor(src.unrealized_pnl) }}>{formatCurrency(src.unrealized_pnl, cur)}</td>
                                        <td className={styles.td} style={{ color: pnlColor(src.return_pct), fontWeight: 700 }}>{formatPct(src.return_pct)}</td>
                                        <td className={styles.td}>{src.trade_count ? `${src.trade_count} 筆 / ${formatValue((src.win_count / src.trade_count) * 100, 0)}%` : '—'}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>{formatCurrency(src.initial_capital, cur)}（{src.initial_basis ?? '—'}）</td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 2b. Exit-reason anatomy: why long strategies lose */}
            {exitStats.length > 0 && (
                <div className={styles.tableCard}>
                    <div className={styles.tableHeader}>
                        <div className={styles.sectionTitle}>
                            <ShieldAlert size={14} style={{ color: '#f6c453' }} />
                            策略出場原因分析：停損、時間到期、停利各佔多少
                        </div>
                        <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>停利 = 達到目標價；損益為已扣費用</span>
                    </div>
                    <div className={styles.tableWrapper}>
                        <table className={styles.dataTable}>
                            <thead>
                                <tr>
                                    <th className={styles.th}>策略</th>
                                    <th className={styles.th}>交易數</th>
                                    <th className={styles.th}>停利達成</th>
                                    <th className={styles.th}>硬停損</th>
                                    <th className={styles.th}>移動停損</th>
                                    <th className={styles.th}>時間到期</th>
                                    <th className={styles.th}>勝率</th>
                                    <th className={styles.th}>合計損益</th>
                                </tr>
                            </thead>
                            <tbody>
                                {exitStats.map(r => (
                                    <tr key={r.strategy}>
                                        <td className={styles.td}><strong>{r.strategy}</strong> <span style={{ color: '#8b94a7' }}>({r.currency})</span></td>
                                        <td className={styles.td}>{r.total}</td>
                                        {(['take_profit', 'hard_stop', 'trailing_stop', 'time_stop'] as const).map(k => (
                                            <td key={k} className={styles.td} style={{ color: k === 'take_profit' ? '#8bd6a5' : undefined }}>
                                                {r.by[k]?.n ?? 0}（{formatValue(((r.by[k]?.n ?? 0) / r.total) * 100, 0)}%）
                                                <span style={{ color: (r.by[k]?.pnl ?? 0) >= 0 ? '#8bd6a5' : '#ff8c8c', marginLeft: 4, fontSize: '0.68rem' }}>{(r.by[k]?.pnl ?? 0) >= 0 ? '+' : ''}{formatValue(r.by[k]?.pnl ?? 0, 0)}</span>
                                            </td>
                                        ))}
                                        <td className={styles.td}>{formatValue(r.winRate, 0)}%</td>
                                        <td className={styles.td} style={{ color: r.pnl >= 0 ? '#8bd6a5' : '#ff8c8c', fontWeight: 700 }}>{r.pnl >= 0 ? '+' : ''}{formatValue(r.pnl, 0)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

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
                            {zh(posture?.posture_label)}
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
                                <span style={{ fontSize: '0.68rem', color: posture?.tw_regime?.regime && posture.tw_regime.regime !== '—' && posture.tw_regime.regime !== 'UNKNOWN' && posture.tw_regime.regime !== 'UNAVAILABLE' ? '#8bd6a5' : '#8b94a7' }}>
                                    {zh(posture?.tw_regime?.regime)}
                                </span>
                            </div>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7', marginTop: 2 }}>
                                趨勢: {zh(posture?.tw_regime?.trend)}
                            </span>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                波動: {zh(posture?.tw_regime?.volatility)}
                            </span>
                        </div>

                        {/* US Regime */}
                        <div className={styles.regimeItem}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <strong style={{ fontSize: '0.76rem' }}>美股體系 (US Regime)</strong>
                                <span style={{ fontSize: '0.68rem', color: posture?.us_regime?.regime && posture.us_regime.regime !== '—' && posture.us_regime.regime !== 'UNKNOWN' && posture.us_regime.regime !== 'UNAVAILABLE' ? '#f6c453' : '#8b94a7' }}>
                                    {zh(posture?.us_regime?.regime)}
                                </span>
                            </div>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7', marginTop: 2 }}>
                                趨勢: {zh(posture?.us_regime?.trend)}
                            </span>
                            <span style={{ fontSize: '0.72rem', color: '#8b94a7' }}>
                                波動: {zh(posture?.us_regime?.volatility)}
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
                                    <Lock size={10} style={{ verticalAlign: '-1px' }} /> 保守鎖定（無執行器連線，非實際觸發）
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

            <div data-testid="current-duty" role="status">
                目前職責（最後紀錄，非執行中證明）：{' '}
                {desk?.current_duty
                    ? `${desk.current_duty.source} · ${desk.current_duty.task} · ${desk.current_duty.observed_at} · ${desk.current_duty.freshness}`
                    : 'UNKNOWN — 無可驗證職責紀錄'}
            </div>

            {/* 4. Market Quotes Feed (Source, Timestamp, Freshness) */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div className={styles.sectionTitle}>
                        <Activity size={14} style={{ color: '#3d8bff' }} />
                        核心行情源與即時性 (Market Quotes & Freshness)
                    </div>
                    <label>
                        行情來源篩選{' '}
                        <select aria-label="行情來源篩選" value={selectedQuoteSource}
                            onChange={(event) => setQuoteSource(event.target.value)}>
                            <option value="ALL">全部來源</option>
                            {quoteSources.map((source) => <option key={source} value={source}>{source}</option>)}
                        </select>
                    </label>
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
                            {visibleQuotes.map((q) => (
                                <tr key={`${q.source}-${q.market}-${q.symbol}`}>
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
                            {visibleQuotes.length === 0 && (
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
                                <th className={styles.th}>帳本來源 (Source)</th>
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
                                <tr key={`${h.source ?? 'UNKNOWN'}-${h.market}-${h.symbol}-${h.strategy_id ?? 'UNKNOWN'}`}>
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
                                        {h.strategy_id ?? 'UNKNOWN'}
                                    </td>
                                    <td className={styles.td}>
                                        {h.source ?? 'UNKNOWN'}
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
                                    <td colSpan={11} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                        當前全體團隊無在倉部位 (Zero Paper Positions)
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 6. Orders / Round trips / Fills */}
            <div className={styles.tableCard}>
                <div className={styles.tableHeader}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                        <div className={styles.sectionTitle}>
                            <Clock size={14} style={{ color: '#3d8bff' }} />
                            紙盤交易紀錄 (Paper Trades)
                        </div>
                        <div className={styles.tabGroup}>
                            <button
                                className={`${styles.tabBtn} ${ordersTab === 'trips' ? styles.tabBtnActive : ''}`}
                                onClick={() => setOrdersTab('trips')}
                            >
                                已完成交易 ({trips.length})
                            </button>
                            <button
                                className={`${styles.tabBtn} ${ordersTab === 'orders' ? styles.tabBtnActive : ''}`}
                                onClick={() => setOrdersTab('orders')}
                            >
                                進行中／被拒委託 ({pendingOrders.length})
                            </button>
                            <button
                                className={`${styles.tabBtn} ${ordersTab === 'fills' ? styles.tabBtnActive : ''}`}
                                onClick={() => setOrdersTab('fills')}
                            >
                                逐筆成交明細 ({fills.length})
                            </button>
                        </div>
                    </div>
                </div>

                <div className={styles.tableWrapper}>
                    {ordersTab === 'trips' && (
                        <>
                            <div style={{ padding: '8px 12px', fontSize: '0.78rem', color: '#8b94a7', display: 'flex', gap: 18, flexWrap: 'wrap' }}>
                                <span>一筆 = 一次買進配對一次賣出（先進先出），點列展開看買賣細節。</span>
                                {tripSummary.map(s => (
                                    <span key={s.currency}>
                                        {s.currency}：{s.count} 筆，勝率 {formatValue(s.winRate, 0)}%，合計損益{' '}
                                        <strong style={{ color: s.pnl >= 0 ? '#8bd6a5' : '#ff8c8c' }}>{s.pnl >= 0 ? '+' : ''}{formatValue(s.pnl)}</strong>
                                    </span>
                                ))}
                            </div>
                            <table className={styles.dataTable}>
                                <thead>
                                    <tr>
                                        <th className={styles.th}></th>
                                        <th className={styles.th}>標的</th>
                                        <th className={styles.th}>方向</th>
                                        <th className={styles.th}>買入價</th>
                                        <th className={styles.th}>賣出價</th>
                                        <th className={styles.th}>損益 (PnL)</th>
                                        <th className={styles.th}>策略</th>
                                        <th className={styles.th}>賣出時間</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {trips.slice(0, tripLimit).map((t: RoundTrip) => {
                                        const open = expandedTrip === t.trip_id;
                                        const color = t.pnl >= 0 ? '#8bd6a5' : '#ff8c8c';
                                        return (
                                            <Fragment key={t.trip_id}>
                                                <tr style={{ cursor: 'pointer' }} onClick={() => setExpandedTrip(open ? null : t.trip_id)}>
                                                    <td className={styles.td} style={{ width: 24 }}>{open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}</td>
                                                    <td className={styles.td}><strong>{t.symbol}</strong> <span style={{ color: '#8b94a7' }}>({t.market})</span></td>
                                                    <td className={styles.td} style={{ color: t.direction === 'LONG' ? '#8bd6a5' : '#ff8c8c', fontWeight: 700 }}>{t.direction === 'LONG' ? '做多 LONG' : '做空 SHORT'}</td>
                                                    <td className={styles.td}>{formatValue(t.buy_price)}</td>
                                                    <td className={styles.td}>{formatValue(t.sell_price)}</td>
                                                    <td className={styles.td} style={{ color, fontWeight: 700 }}>
                                                        {t.pnl >= 0 ? '+' : ''}{formatValue(t.pnl)} {t.currency}
                                                        <span style={{ marginLeft: 6, fontWeight: 400 }}>({formatPct(t.pnl_pct)})</span>
                                                    </td>
                                                    <td className={styles.td} style={{ color: '#8b94a7' }}>{t.strategy_id ?? '—'}</td>
                                                    <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>{fmtTripTime(t.sell_time)}</td>
                                                </tr>
                                                {open && (
                                                    <tr key={t.trip_id + ':detail'}>
                                                        <td className={styles.td} colSpan={8} style={{ textAlign: 'left', whiteSpace: 'normal', backgroundColor: 'rgba(61, 139, 255, 0.05)', fontSize: '0.76rem', lineHeight: 1.8 }}>
                                                            <div>
                                                                <strong>買進</strong>（{t.legs.buys.length} 筆成交，共 {formatValue(t.quantity, 0)} 股）：
                                                                {t.legs.buys.map((b, i) => (
                                                                    <span key={i} style={{ marginLeft: 10 }}>
                                                                        {fmtTripTime(b.time)} @ {formatValue(b.price)} × {formatValue(b.qty, 0)}（費 {formatValue(b.fee)}）
                                                                    </span>
                                                                ))}
                                                            </div>
                                                            <div>
                                                                <strong>賣出</strong>：{fmtTripTime(t.legs.sell.time)} @ {formatValue(t.legs.sell.price)} × {formatValue(t.legs.sell.qty, 0)}（費 {formatValue(t.legs.sell.fee)}）
                                                            </div>
                                                            <div>
                                                                <strong>持有</strong> {fmtHold(t.buy_time, t.sell_time)} · 費用合計 {formatValue(t.fees)} · 帳本 {t.source}
                                                            </div>
                                                            <div style={{ color: '#dde3ee' }}>
                                                                <strong>進場理由</strong>：{t.entry_reason ?? '—'}　<strong>出場原因</strong>：{t.exit_reason ?? '—'}
                                                            </div>
                                                        </td>
                                                    </tr>
                                                )}
                                            </Fragment>
                                        );
                                    })}
                                    {trips.length === 0 && (
                                        <tr>
                                            <td colSpan={8} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                                尚無已完成（買進後又賣出）的交易
                                            </td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                            {trips.length > tripLimit && (
                                <div style={{ padding: 10, textAlign: 'center' }}>
                                    <button className={styles.tabBtn} onClick={() => setTripLimit(n => n + 50)}>
                                        再顯示 50 筆（尚有 {trips.length - tripLimit} 筆）
                                    </button>
                                </div>
                            )}
                        </>
                    )}
                    {ordersTab === 'orders' && (
                        <table className={styles.dataTable}>
                            <thead>
                                <tr>
                                    <th className={styles.th}>標的 / 市場</th>
                                    <th className={styles.th}>買賣方向</th>
                                    <th className={styles.th}>數量</th>
                                    <th className={styles.th}>價格</th>
                                    <th className={styles.th}>狀態</th>
                                    <th className={styles.th}>策略來源</th>
                                    <th className={styles.th}>原因</th>
                                    <th className={styles.th}>建立時間</th>
                                </tr>
                            </thead>
                            <tbody>
                                {pendingOrders.map((o: PaperOrder) => (
                                    <tr key={o.order_id}>
                                        <td className={styles.td}><strong>{o.symbol}</strong> ({o.market})</td>
                                        <td className={styles.td} style={{ color: o.side === 'BUY' ? '#8bd6a5' : '#ff8c8c', fontWeight: 700 }}>{o.side}</td>
                                        <td className={styles.td}>{formatValue(o.quantity, 0)}</td>
                                        <td className={styles.td}>{formatValue(o.price)}</td>
                                        <td className={styles.td}>{o.status}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>{o.strategy_id ?? '—'}</td>
                                        <td className={styles.td} style={{ textAlign: 'left', maxWidth: 260, whiteSpace: 'normal', fontSize: '0.72rem' }}>{o.rationale ?? '—'}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>{formatSnapshotTime(o.created_at)}</td>
                                    </tr>
                                ))}
                                {pendingOrders.length === 0 && (
                                    <tr>
                                        <td colSpan={8} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
                                            目前沒有進行中或被拒的委託（紙盤委託皆即時模擬成交，已成交者請看「已完成交易」）
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    )}
                    {ordersTab === 'fills' && (
                        <table className={styles.dataTable}>
                            <thead>
                                <tr>
                                    <th className={styles.th}>成交時間</th>
                                    <th className={styles.th}>標的 / 市場</th>
                                    <th className={styles.th}>買賣方向</th>
                                    <th className={styles.th}>成交數量</th>
                                    <th className={styles.th}>成交價</th>
                                    <th className={styles.th}>手續費</th>
                                    <th className={styles.th}>策略</th>
                                </tr>
                            </thead>
                            <tbody>
                                {fills.map((f: PaperFill) => (
                                    <tr key={f.fill_id}>
                                        <td className={styles.td} style={{ color: '#8b94a7', fontSize: '0.7rem' }}>{formatSnapshotTime(f.timestamp)}</td>
                                        <td className={styles.td}><strong>{f.symbol}</strong> ({f.market})</td>
                                        <td className={styles.td} style={{ color: f.side === 'BUY' ? '#8bd6a5' : '#ff8c8c', fontWeight: 700 }}>{f.side}</td>
                                        <td className={styles.td}>{formatValue(f.quantity, 0)}</td>
                                        <td className={styles.td}>{formatValue(f.price)}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>{formatValue(f.fee)}</td>
                                        <td className={styles.td} style={{ color: '#8b94a7' }}>{f.strategy_id ?? '—'}</td>
                                    </tr>
                                ))}
                                {fills.length === 0 && (
                                    <tr>
                                        <td colSpan={7} style={{ textAlign: 'center', padding: '16px', color: '#8b94a7' }}>
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
