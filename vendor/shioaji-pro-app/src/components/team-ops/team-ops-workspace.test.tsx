// src/components/team-ops/team-ops-workspace.test.tsx
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TeamOpsWorkspace } from './team-ops-workspace';

function render() {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    let r!: ReactTestRenderer;
    act(() => {
        r = create(createElement(TeamOpsWorkspace));
    });
    return r;
}

describe('TeamOpsWorkspace component', () => {
    beforeEach(() => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
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
                                session_label: '台股收盤 (Closed)',
                                timezone: 'Asia/Taipei (UTC+8)',
                                server_time: '2026-09-26T14:39:52Z',
                            },
                            us: {
                                market: 'US',
                                status: 'OPEN',
                                session_label: '美股常規 (09:30–16:00 ET)',
                                timezone: 'America/New_York (ET)',
                                server_time: '2026-09-26T14:39:52Z',
                            },
                        },
                        portfolio: {
                            reporting_currency: 'TWD',
                            equity: 12500000,
                            nav_status: 'fresh',
                            cash: 4200000,
                            realized_pnl: 350000,
                            unrealized_pnl: 150000,
                            return_pct: 4.17,
                            initial_cash: 12000000,
                            as_of: '2026-09-26T14:39:52Z',
                        },
                        holdings: [],
                        posture: {
                            posture: 'DEFENSIVE',
                            posture_label: '保守防禦',
                            rationale: '美股高波動率及台股盤整期，團隊維持高現金水位。',
                            tw_regime: {
                                market: 'TW',
                                regime: 'RANGE_BOUND',
                                trend: '平盤整理',
                                volatility: '中低波動',
                                updated_at: '2026-09-26T14:39:52Z',
                            },
                            us_regime: {
                                market: 'US',
                                regime: 'HIGH_VOLATILITY',
                                trend: '高檔震盪',
                                volatility: '偏高',
                                updated_at: '2026-09-26T14:39:52Z',
                            },
                            target_cash_pct: 35,
                            updated_at: '2026-09-26T14:39:52Z',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: true,
                            budget_usage_pct: 28.5,
                            max_drawdown_pct: 1.82,
                            drawdown_limit_pct: 5.0,
                            daily_loss_limit: 100000,
                            current_daily_loss: 4200,
                            breaches: [],
                        },
                        quotes: [],
                        orders: [],
                        fills: [],
                        activity: [],
                        benchmark: {
                            benchmark_name: 'TAIEX / S&P 500 混合基準',
                            period: '30D',
                            currency: 'TWD',
                            team_return_pct: 4.17,
                            benchmark_return_pct: 2.1,
                            alpha_pct: 2.07,
                            updated_at: '2026-09-26T14:39:52Z',
                        },
                        legacy_leaderboard: [],
                    }),
                    { status: 200 }
                )
            )
        ));
    });

    it('renders explicit PAPER ONLY and BROKER DISCONNECTED badges', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('PAPER ONLY');
        expect(textContent).toContain('BROKER:');
        expect(textContent).toContain('DISCONNECTED');
        expect(textContent).toContain('NO REAL-CAPITAL');
    });

    it('displays TW and US market session badges', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('TW:');
        expect(textContent).toContain('US:');
    });

    it('defaults to Observer mode without broker ticket controls', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('觀察者 (Observer)');
        // Must NOT contain broker ticket placement or live flash order ticket controls
        expect(textContent).not.toContain('送出委託');
        expect(textContent).not.toContain('立即下單');
    });

    it('displays NAV_UNAVAILABLE when NAV is missing rather than 0', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'unavailable',
                        safety: {
                            paper_only: true,
                            broker_connected: false,
                            broker_state: 'DISCONNECTED',
                            autonomous_capital_decisions: false,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'CLOSED', session_label: '台股收盤', timezone: 'Asia/Taipei', server_time: '2026-09-26T14:39:52Z' },
                            us: { market: 'US', status: 'CLOSED', session_label: '美股收盤', timezone: 'America/New_York', server_time: '2026-09-26T14:39:52Z' },
                        },
                        portfolio: {
                            reporting_currency: 'TWD',
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
                            posture: 'DEFENSIVE',
                            posture_label: '保守防禦',
                            rationale: '數據不可用',
                            tw_regime: { market: 'TW', regime: 'UNAVAILABLE', trend: '—', volatility: '—', updated_at: '' },
                            us_regime: { market: 'US', regime: 'UNAVAILABLE', trend: '—', volatility: '—', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: false,
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
                            benchmark_name: 'TAIEX',
                            period: '30D',
                            currency: 'TWD',
                            team_return_pct: null,
                            benchmark_return_pct: null,
                            alpha_pct: null,
                            updated_at: '',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('NAV_UNAVAILABLE');
    });

    it('surfaces contradictory safety values and visibly fails closed', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'fresh',
                        safety: {
                            paper_only: false,
                            broker_connected: true,
                            broker_state: 'CONNECTED',
                            autonomous_capital_decisions: true,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'OPEN', session_label: '台股盤中', timezone: 'Asia/Taipei', server_time: '2026-09-26T14:39:52Z' },
                            us: { market: 'US', status: 'CLOSED', session_label: '美股收盤', timezone: 'America/New_York', server_time: '2026-09-26T14:39:52Z' },
                        },
                        portfolio: {
                            reporting_currency: 'TWD',
                            equity: 10000000,
                            nav_status: 'fresh',
                            cash: 5000000,
                            realized_pnl: 0,
                            unrealized_pnl: 0,
                            return_pct: 0,
                            initial_cash: 10000000,
                            as_of: '2026-09-26T14:39:52Z',
                        },
                        holdings: [],
                        posture: {
                            posture: 'DEFENSIVE',
                            posture_label: '保守防禦',
                            rationale: '風控防禦',
                            tw_regime: { market: 'TW', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            us_regime: { market: 'US', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: true,
                            budget_usage_pct: 10,
                            max_drawdown_pct: 1,
                            drawdown_limit_pct: 5,
                            daily_loss_limit: 10000,
                            current_daily_loss: 0,
                            breaches: [],
                        },
                        quotes: [],
                        orders: [],
                        fills: [],
                        activity: [],
                        benchmark: {
                            benchmark_name: 'TAIEX',
                            period: '30D',
                            currency: 'TWD',
                            team_return_pct: 0,
                            benchmark_return_pct: 0,
                            alpha_pct: 0,
                            updated_at: '',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('FAIL-CLOSED');
        expect(textContent).toContain('SAFETY CONTRADICTION');
        expect(textContent).toContain('NON-PAPER DETECTED');
        expect(textContent).toContain('CRITICAL: AUTONOMOUS CAPITAL ACTIVE');
        expect(textContent).not.toContain('BROKER: DISCONNECTED (NO REAL-CAPITAL)');
    });

    it('proves no local-only kill-switch control exists in Team Ops', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });
        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).not.toContain('觸發紙盤熔斷');
        expect(textContent).not.toContain('解除紙盤鎖定');
        expect(textContent).not.toContain('鎖定下單');
    });

    it('proves Team Ops has no Operator toggle and remains observer-first', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });
        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).not.toContain('操作員 (Operator)');
        expect(textContent).not.toContain('操作員');
    });

    it('proves unavailable benchmark currency stays unavailable and never falls back to portfolio reporting currency', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'fresh',
                        safety: {
                            paper_only: true,
                            broker_connected: false,
                            broker_state: 'DISCONNECTED',
                            autonomous_capital_decisions: false,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'CLOSED', session_label: '台股收盤', timezone: 'Asia/Taipei', server_time: '2026-09-26T14:39:52Z' },
                            us: { market: 'US', status: 'CLOSED', session_label: '美股收盤', timezone: 'America/New_York', server_time: '2026-09-26T14:39:52Z' },
                        },
                        portfolio: {
                            reporting_currency: 'USD',
                            equity: 200000,
                            nav_status: 'fresh',
                            cash: 100000,
                            realized_pnl: 5000,
                            unrealized_pnl: 2000,
                            return_pct: 3.5,
                            initial_cash: 195000,
                            as_of: '2026-09-26T14:39:52Z',
                        },
                        holdings: [],
                        posture: {
                            posture: 'DEFENSIVE',
                            posture_label: '保守防禦',
                            rationale: '市場震盪',
                            tw_regime: { market: 'TW', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            us_regime: { market: 'US', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: true,
                            budget_usage_pct: 20,
                            max_drawdown_pct: 1.5,
                            drawdown_limit_pct: 5,
                            daily_loss_limit: 10000,
                            current_daily_loss: 0,
                            breaches: [],
                        },
                        quotes: [],
                        orders: [],
                        fills: [],
                        activity: [],
                        benchmark: {
                            benchmark_name: 'Custom Multi-Asset Benchmark',
                            period: 'Q3-2026',
                            currency: '—',
                            team_return_pct: 3.5,
                            benchmark_return_pct: 1.8,
                            alpha_pct: 1.7,
                            updated_at: '2026-09-26T14:39:52Z',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('Custom Multi-Asset Benchmark');
        expect(textContent).toContain('Q3-2026');
        expect(textContent).toContain('USD');
        expect(textContent).toContain('貨幣計價: —');
        expect(textContent).not.toContain('貨幣計價: USD');
    });

    it('displays backend-provided benchmark period and currency with no relabeling tab buttons', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'fresh',
                        safety: {
                            paper_only: true,
                            broker_connected: false,
                            broker_state: 'DISCONNECTED',
                            autonomous_capital_decisions: false,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'CLOSED', session_label: '台股收盤', timezone: 'Asia/Taipei', server_time: '2026-09-26T14:39:52Z' },
                            us: { market: 'US', status: 'CLOSED', session_label: '美股收盤', timezone: 'America/New_York', server_time: '2026-09-26T14:39:52Z' },
                        },
                        portfolio: {
                            reporting_currency: 'USD',
                            equity: 200000,
                            nav_status: 'fresh',
                            cash: 100000,
                            realized_pnl: 5000,
                            unrealized_pnl: 2000,
                            return_pct: 3.5,
                            initial_cash: 195000,
                            as_of: '2026-09-26T14:39:52Z',
                        },
                        holdings: [],
                        posture: {
                            posture: 'DEFENSIVE',
                            posture_label: '保守防禦',
                            rationale: '市場震盪',
                            tw_regime: { market: 'TW', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            us_regime: { market: 'US', regime: 'RANGE', trend: '—', volatility: '—', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: true,
                            budget_usage_pct: 20,
                            max_drawdown_pct: 1.5,
                            drawdown_limit_pct: 5,
                            daily_loss_limit: 10000,
                            current_daily_loss: 0,
                            breaches: [],
                        },
                        quotes: [],
                        orders: [],
                        fills: [],
                        activity: [],
                        benchmark: {
                            benchmark_name: 'Custom Multi-Asset Benchmark',
                            period: 'Q3-2026',
                            currency: 'USD',
                            team_return_pct: 3.5,
                            benchmark_return_pct: 1.8,
                            alpha_pct: 1.7,
                            updated_at: '2026-09-26T14:39:52Z',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('Custom Multi-Asset Benchmark');
        expect(textContent).toContain('Q3-2026');
        expect(textContent).toContain('USD');
        // No fake period selection buttons that relabel unchanged data
        expect(textContent).not.toContain('"30D"');
        expect(textContent).not.toContain('"MTD"');
        expect(textContent).not.toContain('"YTD"');
    });

    it('preserves missing numeric values (cash, drawdown, budget, breaches, P&L) as unavailable and never as zero or safe', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'unavailable',
                        safety: {
                            paper_only: true,
                            broker_connected: false,
                            broker_state: 'DISCONNECTED',
                            autonomous_capital_decisions: false,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'UNAVAILABLE', session_label: 'Unavailable', timezone: 'Asia/Taipei', server_time: '' },
                            us: { market: 'US', status: 'UNAVAILABLE', session_label: 'Unavailable', timezone: 'America/New_York', server_time: '' },
                        },
                        portfolio: {
                            reporting_currency: 'TWD',
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
                            posture_label: 'Unavailable',
                            rationale: '數據不可用',
                            tw_regime: { market: 'TW', regime: 'UNAVAILABLE', trend: '—', volatility: '—', updated_at: '' },
                            us_regime: { market: 'US', regime: 'UNAVAILABLE', trend: '—', volatility: '—', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: false,
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
                            currency: 'TWD',
                            team_return_pct: null,
                            benchmark_return_pct: null,
                            alpha_pct: null,
                            updated_at: '',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        // Must NOT render 0 件 (安全)
        expect(textContent).not.toContain('0 件 (安全)');
        // Must NOT render 風控防護正常 (ARMED) when data is unavailable
        expect(textContent).not.toContain('風控防護正常 (ARMED)');
        expect(textContent).toContain('風控狀態不可用 (UNAVAILABLE)');

        // Missing values must render as — or NAV_UNAVAILABLE
        expect(textContent).toContain('NAV_UNAVAILABLE');
    });

    it('handles endpoint failure with unavailable-only state, integrity warnings, and no hardcoded tickers', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.reject(new Error('Backend offline / 503 Service Unavailable'))
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('SAFETY UNAVAILABLE');
        expect(textContent).toContain('BROKER UNKNOWN');
        expect(textContent).not.toContain('PAPER ONLY');
        expect(textContent).not.toContain('BROKER: DISCONNECTED');
        expect(textContent).toContain('NAV_UNAVAILABLE');
        expect(textContent).toContain('數據完整性警告');
        expect(textContent).toContain('Endpoint request failed');
        expect(textContent).toContain('安全狀態衝突警告');

        // Defaults must not be fabricated
        expect(textContent).not.toContain('TWD');
        expect(textContent).not.toContain('台股盤中');
        expect(textContent).not.toContain('美股常規');
        expect(textContent).not.toContain('DEFENSIVE');
        expect(textContent).not.toContain('RANGE_BOUND');
        expect(textContent).not.toContain('CONSOLIDATION');
        expect(textContent).not.toContain('暫無團隊論據輸入');

        // Absolutely no hardcoded tickers unless supplied by backend
        expect(textContent).not.toContain('2330');
        expect(textContent).not.toContain('2454');
        expect(textContent).not.toContain('NVDA');
        expect(textContent).not.toContain('MSFT');
    });

    it('handles schema validation failure by rendering SAFETY UNAVAILABLE, BROKER UNKNOWN, integrity alert and no fabricated defaults', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'fresh',
                        // Malformed: missing required fields
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('SAFETY UNAVAILABLE');
        expect(textContent).toContain('BROKER UNKNOWN');
        expect(textContent).not.toContain('PAPER ONLY');
        expect(textContent).not.toContain('BROKER: DISCONNECTED');
        expect(textContent).toContain('數據完整性警告');
        expect(textContent).toContain('Schema validation failed');
        expect(textContent).not.toContain('台股盤中');
        expect(textContent).not.toContain('美股常規');
        expect(textContent).not.toContain('DEFENSIVE');
        expect(textContent).not.toContain('RANGE_BOUND');
        expect(textContent).not.toContain('CONSOLIDATION');
        expect(textContent).not.toContain('暫無團隊論據輸入');
        expect(textContent).not.toContain('TWD');
    });

    it('never defaults missing reporting currency to TWD and never defaults missing session/posture to open or defensive', async () => {
        vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
            Promise.resolve(
                new Response(
                    JSON.stringify({
                        server_time: '2026-09-26T14:39:52Z',
                        data_freshness: 'fresh',
                        safety: {
                            paper_only: true,
                            broker_connected: false,
                            broker_state: 'DISCONNECTED',
                            autonomous_capital_decisions: false,
                            kill_switch_active: false,
                        },
                        sessions: {
                            tw: { market: 'TW', status: 'CLOSED', session_label: '', timezone: 'Asia/Taipei', server_time: '2026-09-26T14:39:52Z' },
                            us: { market: 'US', status: 'CLOSED', session_label: '', timezone: 'America/New_York', server_time: '2026-09-26T14:39:52Z' },
                        },
                        portfolio: {
                            reporting_currency: '',
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
                            posture: '',
                            posture_label: '',
                            rationale: '',
                            tw_regime: { market: 'TW', regime: '', trend: '', volatility: '', updated_at: '' },
                            us_regime: { market: 'US', regime: '', trend: '', volatility: '', updated_at: '' },
                            updated_at: '',
                        },
                        risk: {
                            kill_switch_active: false,
                            kill_switch_armed: true,
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
                            currency: '',
                            team_return_pct: null,
                            benchmark_return_pct: null,
                            alpha_pct: null,
                            updated_at: '',
                        },
                    }),
                    { status: 200 }
                )
            )
        ));

        let r!: ReactTestRenderer;
        await act(async () => {
            r = render();
            await new Promise((resolve) => setTimeout(resolve, 50));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).not.toContain('TWD');
        expect(textContent).not.toContain('台股盤中');
        expect(textContent).not.toContain('美股常規');
        expect(textContent).not.toContain('DEFENSIVE');
        expect(textContent).not.toContain('RANGE_BOUND');
        expect(textContent).not.toContain('CONSOLIDATION');
        expect(textContent).not.toContain('暫無團隊論據輸入');
    });
});
