// src/components/hud-header.test.tsx
import { vi } from 'vitest';

vi.hoisted(() => {
    vi.stubGlobal('window', new EventTarget());
});

import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { beforeEach, describe, expect, it } from 'vitest';

vi.mock('../lib/tauri', () => ({
    appVersion: vi.fn().mockResolvedValue('1.0.0'),
    checkForUpdates: vi.fn(),
    listenTrayEvents: vi.fn().mockResolvedValue(() => {}),
    openFlashTiles: vi.fn(),
}));

vi.mock('../lib/shioaji', () => ({
    fetchInfo: vi.fn().mockResolvedValue({ simulation: true }),
}));

vi.mock('../hooks/use-stream', () => ({
    useStreamStatus: () => 'live',
}));

vi.mock('./server-manager', () => ({
    ServerManager: () => null,
}));

vi.mock('./settings-dialog', () => ({
    SettingsDialog: () => null,
}));

vi.mock('./layout-library', () => ({
    LayoutLibrary: () => null,
}));

vi.mock('./market-bar', () => ({
    MarketBar: () => null,
}));

import { HudHeader } from './hud-header';

describe('HudHeader component', () => {
    const baseWorkspace = {
        id: 'default',
        name: 'Default',
        blocks: [],
        active: true,
    };

    const renderHeader = (props: Partial<Parameters<typeof HudHeader>[0]> = {}) => {
        vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
        let r!: ReactTestRenderer;
        act(() => {
            r = create(
                createElement(HudHeader, {
                    profiles: [],
                    currentWorkspace: baseWorkspace,
                    onSaveProfile: vi.fn(),
                    onLoadProfile: vi.fn(),
                    onDeleteProfile: vi.fn(),
                    onRenameProfile: vi.fn(),
                    onResetWorkspace: vi.fn(),
                    onLoadPreset: vi.fn(),
                    onOpenPanelLibrary: vi.fn(),
                    ...props,
                })
            );
        });
        return r;
    };

    it('does not render hardcoded safety truth badges (PAPER ONLY or BROKER DISCONNECTED)', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = renderHeader({ activeView: 'team-ops' });
            await new Promise((resolve) => setTimeout(resolve, 10));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).not.toContain('PAPER ONLY');
        expect(textContent).not.toContain('BROKER DISCONNECTED');
    });

    it('does not render unsupported local kill-switch button or risk mutation controls', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = renderHeader({ activeView: 'team-ops' });
            await new Promise((resolve) => setTimeout(resolve, 10));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).not.toContain('風控鎖定');
        expect(textContent).not.toContain('Kill Switch');
        expect(textContent).not.toContain('鎖定下單');
        expect(textContent).not.toContain('killHeaderOn');
    });

    it('remains observer-first and does not present an operator mode toggle', async () => {
        let r!: ReactTestRenderer;
        await act(async () => {
            r = renderHeader({ activeView: 'team-ops' });
            await new Promise((resolve) => setTimeout(resolve, 10));
        });

        const textContent = JSON.stringify(r.toJSON());
        expect(textContent).toContain('觀察者模式 (Observer)');
        expect(textContent).not.toContain('操作員模式 (Operator)');
        expect(textContent).not.toContain('操作員 (Operator)');
    });
});
