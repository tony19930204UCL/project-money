// src/lib/cio-api.test.ts
import { describe, expect, it, vi } from 'vitest';
import {
    cioApi,
    formatCurrency,
    formatPct,
    formatValue,
} from './cio-api';
import type { TeamOpsSnapshot } from './types/team-ops';

describe('cioApi teamOps client & formatting', () => {
    describe('Safe formatting helpers', () => {
        it('never formats null, undefined, or NaN as zero', () => {
            expect(formatValue(null)).toBe('—');
            expect(formatValue(undefined)).toBe('—');
            expect(formatValue(NaN)).toBe('—');

            expect(formatPct(null)).toBe('—');
            expect(formatPct(undefined)).toBe('—');
            expect(formatPct(NaN)).toBe('—');

            expect(formatCurrency(null)).toBe('—');
            expect(formatCurrency(undefined)).toBe('—');
            expect(formatCurrency(NaN)).toBe('—');
            expect(formatCurrency(100)).toBe('100.00');
            expect(formatCurrency(100, undefined)).toBe('100.00');
            expect(formatCurrency(100, '—')).toBe('100.00');
        });

        it('formats legitimate zero correctly', () => {
            expect(formatValue(0)).toBe('0.00');
            expect(formatPct(0)).toBe('0.00%');
            expect(formatCurrency(0, 'TWD')).toBe('TWD 0.00');
            expect(formatCurrency(0)).toBe('0.00');
            expect(formatCurrency(0, '—')).toBe('0.00');
        });

        it('formats numbers with proper signs and decimals', () => {
            expect(formatValue(1234567.89)).toBe('1,234,567.89');
            expect(formatPct(2.5)).toBe('+2.50%');
            expect(formatPct(-1.42)).toBe('-1.42%');
            expect(formatCurrency(50000, 'USD')).toBe('USD 50,000.00');
        });
    });

    describe('cioApi.teamOps client', () => {
        it('returns direct TeamOpsSnapshot when backend endpoint succeeds', async () => {
            const mockSnapshot: TeamOpsSnapshot = {
                server_time: '2026-09-26T14:00:00Z',
                data_freshness: 'fresh',
                safety: {
                    paper_only: true,
                    broker_connected: false,
                    broker_state: 'DISCONNECTED',
                    autonomous_capital_decisions: false,
                    kill_switch_active: false,
                },
                sessions: {
                    tw: {
                        market: 'TW',
                        status: 'CLOSED',
                        session_label: '台股收盤',
                        timezone: 'Asia/Taipei',
                        server_time: '2026-09-26T14:00:00Z',
                    },
                    us: {
                        market: 'US',
                        status: 'PRE_MARKET',
                        session_label: '美股盤前',
                        timezone: 'America/New_York',
                        server_time: '2026-09-26T14:00:00Z',
                    },
                },
                portfolio: {
                    reporting_currency: 'TWD',
                    equity: 5120000,
                    nav_status: 'fresh',
                    cash: 2500000,
                    realized_pnl: 120000,
                    unrealized_pnl: 45000,
                    return_pct: 2.4,
                    initial_cash: 5000000,
                    as_of: '2026-09-26T14:00:00Z',
                },
                holdings: [],
                posture: {
                    posture: 'DEFENSIVE',
                    posture_label: '保守防禦',
                    rationale: '盤整行情，控制回撤',
                    tw_regime: {
                        market: 'TW',
                        regime: 'CONSOLIDATION',
                        trend: '平盤',
                        volatility: '低',
                        updated_at: '2026-09-26T14:00:00Z',
                    },
                    us_regime: {
                        market: 'US',
                        regime: 'TECH_MOMENTUM',
                        trend: '高檔',
                        volatility: '中',
                        updated_at: '2026-09-26T14:00:00Z',
                    },
                    updated_at: '2026-09-26T14:00:00Z',
                },
                risk: {
                    kill_switch_active: false,
                    kill_switch_armed: true,
                    budget_usage_pct: 35.0,
                    max_drawdown_pct: 1.2,
                    drawdown_limit_pct: 5.0,
                    daily_loss_limit: 100000,
                    current_daily_loss: 2000,
                    breaches: [],
                },
                quotes: [],
                orders: [],
                fills: [],
                activity: [],
                benchmark: {
                    benchmark_name: 'TAIEX / S&P 500',
                    period: '30D',
                    currency: 'TWD',
                    team_return_pct: 2.4,
                    benchmark_return_pct: 1.1,
                    alpha_pct: 1.3,
                    updated_at: '2026-09-26T14:00:00Z',
                },
            };

            const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementationOnce((url) => {
                expect(url).toBe('/api/paper/team-ops');
                return Promise.resolve(new Response(JSON.stringify(mockSnapshot), { status: 200 }));
            });

            const result = await cioApi.teamOps();
            expect(fetchSpy).toHaveBeenCalledTimes(1);
            expect(result.safety.paper_only).toBe(true);
            expect(result.safety.broker_connected).toBe(false);
            expect(result.safety.broker_state).toBe('DISCONNECTED');
            expect(result.safety.autonomous_capital_decisions).toBe(false);
            expect(result.portfolio.equity).toBe(5120000);
            expect(result.portfolio.nav_status).toBe('fresh');
        });

        it('fetches direct /api/paper/team-ops without legacy fallback to competition or overview', async () => {
            const fetchCalls: string[] = [];
            vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
                fetchCalls.push(String(url));
                return Promise.reject(new Error('500 Internal Server Error'));
            });

            await cioApi.teamOps();
            expect(fetchCalls).toEqual(['/api/paper/team-ops']);
            expect(fetchCalls).not.toContain('/api/paper/competition');
            expect(fetchCalls).not.toContain('/api/overview');
        });

        it('produces unavailable-only snapshot without fabricated healthy safety values on endpoint failure', async () => {
            vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
                Promise.reject(new Error('Network error / 404 Not Found'))
            );

            const result = await cioApi.teamOps();
            // Critical: unavailable safety is not a healthy safety state!
            // Do not fabricate paper_only=true, broker_connected=false, broker_state=DISCONNECTED, autonomous_capital_decisions=false, kill_switch_active=false
            expect(result.safety.paper_only).toBeNull();
            expect(result.safety.broker_connected).toBeNull();
            expect(result.safety.broker_state).toBeNull();
            expect(result.safety.autonomous_capital_decisions).toBeNull();
            expect(result.safety.kill_switch_active).toBeNull();
            expect(result.risk.kill_switch_active).toBeNull();
            expect(result.risk.kill_switch_armed).toBeNull();

            // Reporting currency must be '—', never 'TWD'
            expect(result.portfolio.reporting_currency).toBe('—');
            expect(result.benchmark.currency).toBe('—');

            // Session labels must be unavailable, never open session defaults
            expect(result.sessions.tw.session_label).toBe('—');
            expect(result.sessions.us.session_label).toBe('—');

            // Posture / regimes / rationale must be unavailable, never DEFENSIVE / RANGE_BOUND / CONSOLIDATION
            expect(result.posture.posture_label).toBe('—');
            expect(result.posture.rationale).toBe('—');
            expect(result.posture.tw_regime.regime).toBe('—');
            expect(result.posture.us_regime.regime).toBe('—');

            // Freshness and NAV status must indicate unavailable, never fresh or 0
            expect(result.data_freshness).toBe('unavailable');
            expect(result.portfolio.nav_status).toBe('NAV_UNAVAILABLE');
            expect(result.portfolio.equity).toBeNull();
            expect(result.portfolio.cash).toBeNull();
            expect(result.portfolio.realized_pnl).toBeNull();
            expect(result.portfolio.unrealized_pnl).toBeNull();
            expect(result.portfolio.return_pct).toBeNull();
            expect(result.risk.budget_usage_pct).toBeNull();
            expect(result.risk.max_drawdown_pct).toBeNull();

            // All collections must be empty
            expect(result.holdings).toEqual([]);
            expect(result.quotes).toEqual([]);
            expect(result.orders).toEqual([]);
            expect(result.fills).toEqual([]);
            expect(result.activity).toEqual([]);
            expect(result.risk.breaches).toEqual([]);

            // Integrity warning must be explicitly present
            expect(result.integrity_warnings).toBeDefined();
            expect(result.integrity_warnings!.length).toBeGreaterThan(0);
            expect(result.integrity_warnings![0]).toContain('Endpoint request failed');

            // No fabricated tickers anywhere
            const textContent = JSON.stringify(result);
            expect(textContent).not.toContain('2330');
            expect(textContent).not.toContain('2454');
            expect(textContent).not.toContain('NVDA');
            expect(textContent).not.toContain('MSFT');
        });

        it('produces unavailable-only snapshot without fabricated healthy safety values on schema validation failure', async () => {
            // Missing required fields like portfolio, sessions, etc.
            const invalidPayload = {
                server_time: '2026-09-26T14:00:00Z',
                data_freshness: 'fresh',
                safety: { paper_only: true },
                // missing portfolio, sessions, risk, etc.
            };

            vi.spyOn(globalThis, 'fetch').mockImplementationOnce(() =>
                Promise.resolve(new Response(JSON.stringify(invalidPayload), { status: 200 }))
            );

            const result = await cioApi.teamOps();
            expect(result.safety.paper_only).toBeNull();
            expect(result.safety.broker_connected).toBeNull();
            expect(result.safety.broker_state).toBeNull();
            expect(result.safety.autonomous_capital_decisions).toBeNull();
            expect(result.safety.kill_switch_active).toBeNull();
            expect(result.risk.kill_switch_active).toBeNull();
            expect(result.risk.kill_switch_armed).toBeNull();

            expect(result.portfolio.reporting_currency).toBe('—');
            expect(result.benchmark.currency).toBe('—');
            expect(result.sessions.tw.session_label).toBe('—');
            expect(result.sessions.us.session_label).toBe('—');
            expect(result.posture.posture_label).toBe('—');
            expect(result.posture.rationale).toBe('—');
            expect(result.posture.tw_regime.regime).toBe('—');
            expect(result.posture.us_regime.regime).toBe('—');

            expect(result.data_freshness).toBe('unavailable');
            expect(result.portfolio.nav_status).toBe('NAV_UNAVAILABLE');
            expect(result.portfolio.equity).toBeNull();
            expect(result.holdings).toEqual([]);
            expect(result.quotes).toEqual([]);
            expect(result.orders).toEqual([]);
            expect(result.fills).toEqual([]);
            expect(result.activity).toEqual([]);

            expect(result.integrity_warnings).toBeDefined();
            expect(result.integrity_warnings![0]).toContain('Schema validation failed');

            const textContent = JSON.stringify(result);
            expect(textContent).not.toContain('2330');
            expect(textContent).not.toContain('2454');
            expect(textContent).not.toContain('NVDA');
            expect(textContent).not.toContain('MSFT');
        });
    });
});
