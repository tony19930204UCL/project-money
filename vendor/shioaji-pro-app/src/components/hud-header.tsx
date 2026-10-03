// src/components/hud-header.tsx — top status bar. 主題/帳號/版面/風控規則
// 都收斂進統一設定 dialog（settings-dialog.tsx）；header 只留高頻操作：
// 伺服器狀態、Kill Switch（一鍵鎖定/解鎖）、新增面板、閃電全開、設定。

import { LayoutGrid, Settings, Zap } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useStreamStatus } from '../hooks/use-stream';
import { useHeaderItems } from '../lib/header-items';
import { fetchInfo } from '../lib/shioaji';
import { maskMoney, usePrivacyMoney } from '../lib/privacy';
import {
    appVersion,
    checkForUpdates,
    listenTrayEvents,
    openFlashTiles,
    type FlashTileLayout,
} from '../lib/tauri';
import { fmtMoney } from '../lib/utils/format';
import type { Profile, Workspace } from '../lib/workspace';
import { LayoutLibrary } from './layout-library';
import { MarketBar } from './market-bar';
import { ServerManager } from './server-manager';
import { SettingsDialog } from './settings-dialog';
import { getTwSession, getUsSession } from '../lib/market-session';
import * as styles from './hud-header.css';

const STATUS_LABEL = {
    live: 'DATA: FRESH',
    connecting: 'DATA: SYNC',
    down: 'DATA: LOST',
    stale: 'DATA: STALE',
} as const;

function Menu({
    label,
    children,
}: {
    label: React.ReactNode;
    children: (close: () => void) => React.ReactNode;
}) {
    const [open, setOpen] = useState(false);
    return (
        <div className={styles.settingsWrap}>
            <button
                className={styles.resetBtn}
                onClick={() => setOpen((o) => !o)}
            >
                {label}
            </button>
            {open && (
                <>
                    <div
                        className={styles.popoverBackdrop}
                        onClick={() => setOpen(false)}
                    />
                    <div className={styles.popover}>
                        {children(() => setOpen(false))}
                    </div>
                </>
            )}
        </div>
    );
}

// ⚡全開 layout picker: thumbnails first, windows open per the chosen
// arrangement. Flash ladders are tall and narrow, so the practical
// layouts are full-height strips side by side.
const FLASH_LAYOUTS: (FlashTileLayout & { key: string; label: string })[] = [
    { key: 's8', label: '8 長條', cols: 8, rows: 1, region: 'full' },
    { key: 's6', label: '6 長條', cols: 6, rows: 1, region: 'full' },
    { key: 's4', label: '4 長條', cols: 4, rows: 1, region: 'full' },
    { key: 's3', label: '3 長條', cols: 3, rows: 1, region: 'full' },
    { key: 'right4', label: '右側直欄', cols: 1, rows: 4, region: 'right' },
];

function FlashThumb({ layout }: { layout: FlashTileLayout }) {
    const regionStyle: React.CSSProperties =
        layout.region === 'right'
            ? { top: 1, bottom: 1, right: 1, width: '26%' }
            : layout.region === 'bottom'
              ? { left: 1, right: 1, bottom: 1, height: '34%' }
              : { inset: 1 };
    return (
        <span className={styles.flashThumb}>
            <span
                className={styles.flashThumbRegion}
                style={{
                    ...regionStyle,
                    gridTemplateColumns: `repeat(${layout.cols}, 1fr)`,
                    gridTemplateRows: `repeat(${layout.rows}, 1fr)`,
                }}
            >
                {Array.from({ length: layout.cols * layout.rows }).map(
                    (_, i) => (
                        <span key={i} className={styles.flashThumbCell} />
                    ),
                )}
            </span>
        </span>
    );
}

function FlashTilesMenu({ flashCodes }: { flashCodes: string[] }) {
    return (
        <Menu
            label={
                <>
                    <Zap size={11} style={{ verticalAlign: '-1px' }} /> 全開
                </>
            }
        >
            {(close) => (
                <>
                    <span className={styles.settingLabel}>
                        閃電下單全開 — 選擇排版
                    </span>
                    {FLASH_LAYOUTS.map((lay) => {
                        const count = Math.min(
                            flashCodes.length,
                            lay.cols * lay.rows,
                        );
                        return (
                            <button
                                key={lay.key}
                                className={styles.flashLayoutItem}
                                onClick={() => {
                                    close();
                                    void openFlashTiles(flashCodes, lay);
                                }}
                            >
                                <FlashThumb layout={lay} />
                                <span className={styles.flashLayoutLabel}>
                                    {lay.label}
                                    <span className={styles.presetDesc}>
                                        自選前 {count} 檔
                                    </span>
                                </span>
                            </button>
                        );
                    })}
                </>
            )}
        </Menu>
    );
}

export function HudHeader({
    accBalance,
    onOpenPanelLibrary,
    profiles,
    currentWorkspace,
    onSaveProfile,
    onLoadProfile,
    onDeleteProfile,
    onRenameProfile,
    onResetWorkspace,
    onLoadPreset,
    flashCodes = [],
    activeView = 'team-ops',
    onChangeView,
}: {
    accBalance?: number;
    onOpenPanelLibrary: () => void;
    flashCodes?: string[];
    profiles: Profile[];
    currentWorkspace: Workspace;
    onSaveProfile: (name: string, icon?: string) => void;
    onLoadProfile: (name: string) => void;
    onDeleteProfile: (name: string) => void;
    onRenameProfile: (oldName: string, newName: string) => void;
    onResetWorkspace: () => void;
    onLoadPreset: (name: string) => void;
    activeView?: 'team-ops' | 'legacy';
    onChangeView?: (view: 'team-ops' | 'legacy') => void;
}) {
    const streamStatus = useStreamStatus();
    const privMoney = usePrivacyMoney();
    const headerItems = useHeaderItems();
    const [simulation, setSimulation] = useState<boolean | null>(null);
    const [appVer, setAppVer] = useState('');
    const [now, setNow] = useState(() => new Date());
    const [serverMgrOpen, setServerMgrOpen] = useState(false);
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [layoutLibOpen, setLayoutLibOpen] = useState(false);

    const twSession = getTwSession(now);
    const usSession = getUsSession(now);

    useEffect(() => {
        let cleanup: (() => void) | undefined;
        listenTrayEvents(() => setServerMgrOpen(true)).then((un) => {
            cleanup = un;
        });
        const t = setTimeout(() => checkForUpdates(true), 8000);
        return () => {
            cleanup?.();
            clearTimeout(t);
        };
    }, []);

    useEffect(() => {
        void appVersion().then(setAppVer);
    }, []);

    useEffect(() => {
        // retry until the server answers — a one-shot fetch loses the race
        // against a daemon that is still starting after an app update
        let done = false;
        const load = () =>
            fetchInfo()
                .then((info) => {
                    done = true;
                    clearInterval(retry);
                    setSimulation(info.simulation);
                })
                .catch(() => undefined);
        const retry = setInterval(() => {
            if (!done) void load();
        }, 5000);
        void load();
        const t = setInterval(() => setNow(new Date()), 1000);
        return () => {
            clearInterval(t);
            clearInterval(retry);
        };
    }, []);

    return (
        <header className={styles.header}>
            <div className={styles.logoBlock}>
                <span className={styles.logoMain}>Project Money</span>
                <span className={styles.logoSub}>
                    團隊運維
                    {appVer && ` · App ${appVer}`}
                </span>
            </div>


            {/* TW & US Session Badges */}
            <span className={styles.chip} title={`台股時區: ${twSession.timezone}`}>
                <span className={twSession.isOpen ? styles.led.live : styles.led.down} />
                <span>TW: {twSession.label}</span>
            </span>
            <span className={styles.chip} title={`美股時區: ${usSession.timezone}`}>
                <span className={usSession.isOpen ? styles.led.live : styles.led.down} />
                <span>US: {usSession.label}</span>
            </span>

            {/* Workspace View Switcher: Team Ops is default */}
            <div style={{ display: 'inline-flex', gap: 2, padding: 2, backgroundColor: 'rgba(0,0,0,0.3)', borderRadius: 4, border: '1px solid rgba(255,255,255,0.08)' }}>
                <button
                    className={styles.resetBtn}
                    style={{
                        backgroundColor: activeView === 'team-ops' ? 'rgba(61, 139, 255, 0.2)' : 'transparent',
                        color: activeView === 'team-ops' ? '#3d8bff' : '#8b94a7',
                        fontWeight: activeView === 'team-ops' ? 700 : 500,
                        padding: '2px 8px',
                    }}
                    onClick={() => onChangeView?.('team-ops')}
                >
                    團隊運維 (Team Ops)
                </button>
                <button
                    className={styles.resetBtn}
                    style={{
                        backgroundColor: activeView === 'legacy' ? 'rgba(61, 139, 255, 0.2)' : 'transparent',
                        color: activeView === 'legacy' ? '#3d8bff' : '#8b94a7',
                        fontWeight: activeView === 'legacy' ? 700 : 500,
                        padding: '2px 8px',
                    }}
                    onClick={() => onChangeView?.('legacy')}
                >
                    傳統終端 (Terminal)
                </button>
            </div>

            {/* Observer Mode Badge */}
            <span
                className={styles.chip}
                style={{ color: '#8bd6a5', borderColor: 'rgba(139, 214, 165, 0.3)' }}
                title="觀察者模式：純檢視，無實盤下單控制"
            >
                👀 觀察者模式 (Observer)
            </span>

            <MarketBar />

            <div className={styles.spacer} />

            {headerItems.bankBalance && accBalance !== undefined && (
                <div className={`${styles.chip} ${styles.infoAutoHide.second}`}>
                    <span className={styles.chipLabel}>銀行水位</span>
                    <span>{maskMoney(fmtMoney(accBalance), privMoney)}</span>
                </div>
            )}

            {headerItems.liveStatus && (
                <div className={styles.chip}>
                    <span className={styles.led[streamStatus]} />
                    <span>{STATUS_LABEL[streamStatus]}</span>
                </div>
            )}

            <ServerManager
                open={serverMgrOpen}
                onToggle={setServerMgrOpen}
            />

            {activeView === 'legacy' && (
                <button
                    className={styles.resetBtn}
                    onClick={onOpenPanelLibrary}
                >
                    ＋ 新增面板
                </button>
            )}

            {headerItems.layoutLibrary && activeView === 'legacy' && (
                <button
                    className={styles.resetBtn}
                    title='版面庫（預設版面/我的版面/儲存目前版面）'
                    onClick={() => setLayoutLibOpen(true)}
                >
                    <LayoutGrid size={11} style={{ verticalAlign: '-1px' }} />{' '}
                    版面
                </button>
            )}

            {headerItems.flashAll && flashCodes.length > 0 && activeView === 'legacy' && (
                <FlashTilesMenu flashCodes={flashCodes} />
            )}

            <button
                className={styles.resetBtn}
                title='設定（外觀/音效與隱私/帳號/風控/版面）'
                onClick={() => setSettingsOpen(true)}
            >
                <Settings size={11} style={{ verticalAlign: '-1px' }} /> 設定
            </button>
            <SettingsDialog
                open={settingsOpen}
                onClose={() => setSettingsOpen(false)}
                onResetWorkspace={onResetWorkspace}
                onOpenLayoutLibrary={() => setLayoutLibOpen(true)}
            />
            <LayoutLibrary
                open={layoutLibOpen}
                onClose={() => setLayoutLibOpen(false)}
                profiles={profiles}
                currentWorkspace={currentWorkspace}
                onSaveProfile={onSaveProfile}
                onLoadProfile={onLoadProfile}
                onDeleteProfile={onDeleteProfile}
                onRenameProfile={onRenameProfile}
                onLoadPreset={onLoadPreset}
            />

            {headerItems.clock && (
                <span className={`${styles.clock} ${styles.infoAutoHide.first}`}>
                    {now.toLocaleTimeString('en-GB', { hour12: false })}
                </span>
            )}
        </header>
    );
}
