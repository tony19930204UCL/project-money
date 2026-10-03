// src/components/team-ops/team-ops-workspace.css.ts
import { globalStyle, style } from '@vanilla-extract/css';
import { vars } from '../../theme.css';

export const container = style({
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    width: '100%',
    overflowY: 'auto',
    overflowX: 'hidden',
    backgroundColor: vars.color.background,
    color: vars.color.foreground,
    fontFamily: vars.font.body,
    padding: '12px 16px 32px 16px',
    gap: 14,
    boxSizing: 'border-box',
});

globalStyle(`${container} > *`, {
    flexShrink: 0,
});

// Top Safety & Session Bar
export const safetyBar = style({
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 14px',
    backgroundColor: vars.color.panel,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    gap: 10,
});

export const safetyBadgesGroup = style({
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    gap: 8,
});

export const badgePaperOnly = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '3px 8px',
    borderRadius: 4,
    fontSize: '0.75rem',
    fontWeight: 700,
    letterSpacing: '0.04em',
    color: '#f6c453',
    backgroundColor: 'rgba(246, 196, 83, 0.12)',
    border: '1px solid rgba(246, 196, 83, 0.35)',
});

export const badgeBrokerDisconnected = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '3px 8px',
    borderRadius: 4,
    fontSize: '0.75rem',
    fontWeight: 600,
    color: '#8b94a7',
    backgroundColor: 'rgba(139, 148, 167, 0.10)',
    border: `1px solid ${vars.color.border}`,
});

export const badgeSession = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    padding: '3px 8px',
    borderRadius: 4,
    fontSize: '0.73rem',
    fontWeight: 500,
    color: vars.color.foreground,
    backgroundColor: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
});

export const sessionDotOpen = style({
    width: 7,
    height: 7,
    borderRadius: '50%',
    backgroundColor: vars.color.success,
    boxShadow: `0 0 6px ${vars.color.success}`,
});

export const sessionDotClosed = style({
    width: 7,
    height: 7,
    borderRadius: '50%',
    backgroundColor: vars.color.mutedForeground,
});

export const sessionDotPre = style({
    width: 7,
    height: 7,
    borderRadius: '50%',
    backgroundColor: vars.color.amber,
});

export const badgeFresh = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    padding: '2px 7px',
    borderRadius: 4,
    fontSize: '0.72rem',
    fontWeight: 600,
    color: '#8bd6a5',
    backgroundColor: 'rgba(139, 214, 165, 0.12)',
    border: '1px solid rgba(139, 214, 165, 0.3)',
});

export const badgeStale = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    padding: '2px 7px',
    borderRadius: 4,
    fontSize: '0.72rem',
    fontWeight: 600,
    color: '#f6c453',
    backgroundColor: 'rgba(246, 196, 83, 0.12)',
    border: '1px solid rgba(246, 196, 83, 0.3)',
});

export const badgeUnavailable = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    padding: '2px 7px',
    borderRadius: 4,
    fontSize: '0.72rem',
    fontWeight: 600,
    color: '#ff8c8c',
    backgroundColor: 'rgba(255, 140, 140, 0.12)',
    border: '1px solid rgba(255, 140, 140, 0.3)',
});

export const serverTimestamp = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    fontSize: '0.72rem',
    color: vars.color.mutedForeground,
    fontFamily: vars.font.mono,
});

export const modeToggleBtn = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '4px 10px',
    borderRadius: 4,
    fontSize: '0.75rem',
    fontWeight: 600,
    cursor: 'pointer',
    border: `1px solid ${vars.color.borderBright}`,
    backgroundColor: vars.color.inset,
    color: vars.color.foreground,
    transition: 'all 0.15s ease',
    ':hover': {
        backgroundColor: vars.color.panelRaised,
    },
});

export const modeToggleBtnActive = style({
    backgroundColor: vars.color.accentDim,
    borderColor: vars.color.accent,
    color: vars.color.accent,
});

// Top Metrics Section (Desktop: 4-6 cards row, Mobile: stacked)
export const metricsGrid = style({
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
    gap: 10,
});

export const metricCard = style({
    display: 'flex',
    flexDirection: 'column',
    padding: '10px 14px',
    backgroundColor: vars.color.panel,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    gap: 4,
});

export const metricLabel = style({
    fontSize: '0.72rem',
    fontWeight: 500,
    color: vars.color.mutedForeground,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
});

export const metricValue = style({
    fontSize: '1.25rem',
    fontWeight: 700,
    fontFamily: vars.font.mono,
    lineHeight: 1.2,
});

export const metricSubtext = style({
    fontSize: '0.7rem',
    color: vars.color.mutedForeground,
    marginTop: 2,
});

// Posture & Risk Row
export const middleGrid = style({
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
    gap: 12,
});

export const sectionCard = style({
    display: 'flex',
    flexDirection: 'column',
    padding: '12px 14px',
    backgroundColor: vars.color.panel,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    gap: 10,
});

export const sectionHeader = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBottom: 6,
    borderBottom: `1px solid ${vars.color.border}`,
});

export const sectionTitle = style({
    fontSize: '0.85rem',
    fontWeight: 700,
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    color: vars.color.foreground,
});

export const postureBox = style({
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    padding: '8px 10px',
    backgroundColor: vars.color.inset,
    borderRadius: 4,
    border: `1px solid ${vars.color.border}`,
});

export const rationaleText = style({
    fontSize: '0.78rem',
    lineHeight: 1.45,
    color: vars.color.foreground,
    margin: 0,
});

export const regimeGrid = style({
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 8,
    marginTop: 4,
});

export const regimeItem = style({
    display: 'flex',
    flexDirection: 'column',
    padding: '6px 8px',
    backgroundColor: 'rgba(255,255,255,0.02)',
    border: `1px solid ${vars.color.border}`,
    borderRadius: 4,
    gap: 2,
});

// Risk Section
export const riskMeterContainer = style({
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
});

export const riskProgressBar = style({
    width: '100%',
    height: 6,
    backgroundColor: vars.color.inset,
    borderRadius: 3,
    overflow: 'hidden',
    border: `1px solid ${vars.color.border}`,
});

export const riskProgressFill = style({
    height: '100%',
    transition: 'width 0.3s ease',
});

// Tables & Listings Section
export const tableCard = style({
    display: 'flex',
    flexDirection: 'column',
    flexShrink: 0,
    backgroundColor: vars.color.panel,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    overflow: 'hidden',
});

export const tableHeader = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 14px',
    borderBottom: `1px solid ${vars.color.border}`,
});

export const tableWrapper = style({
    overflowX: 'auto',
    width: '100%',
});

export const dataTable = style({
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: '0.78rem',
    whiteSpace: 'nowrap',
    fontFamily: vars.font.body,
});

export const th = style({
    padding: '8px 10px',
    textAlign: 'right',
    color: vars.color.mutedForeground,
    fontWeight: 600,
    borderBottom: `1px solid ${vars.color.border}`,
    backgroundColor: vars.color.inset,
    ':first-child': {
        textAlign: 'left',
    },
});

export const td = style({
    padding: '7px 10px',
    textAlign: 'right',
    borderBottom: '1px solid rgba(255,255,255,0.04)',
    fontFamily: vars.font.mono,
    ':first-child': {
        textAlign: 'left',
        fontFamily: vars.font.body,
    },
});

export const tabGroup = style({
    display: 'flex',
    gap: 4,
});

export const tabBtn = style({
    padding: '4px 10px',
    fontSize: '0.75rem',
    fontWeight: 600,
    borderRadius: 4,
    border: 'none',
    cursor: 'pointer',
    backgroundColor: 'transparent',
    color: vars.color.mutedForeground,
    ':hover': {
        color: vars.color.foreground,
    },
});

export const tabBtnActive = style({
    backgroundColor: vars.color.inset,
    color: vars.color.foreground,
    border: `1px solid ${vars.color.borderBright}`,
});

export const legacyAccordion = style({
    marginTop: 8,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    backgroundColor: 'rgba(255,255,255,0.015)',
    overflow: 'hidden',
});

export const legacyAccordionHeader = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 14px',
    cursor: 'pointer',
    backgroundColor: vars.color.inset,
    fontSize: '0.75rem',
    color: vars.color.mutedForeground,
    fontWeight: 600,
    ':hover': {
        color: vars.color.foreground,
    },
});

export const badgeSafetyContradiction = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '3px 8px',
    borderRadius: 4,
    fontSize: '0.75rem',
    fontWeight: 700,
    color: '#ff8c8c',
    backgroundColor: 'rgba(255, 60, 60, 0.2)',
    border: '1px solid rgba(255, 60, 60, 0.5)',
});

export const alertBanner = style({
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    padding: '8px 12px',
    borderRadius: 4,
    fontSize: '0.78rem',
    backgroundColor: 'rgba(255, 60, 60, 0.12)',
    border: '1px solid rgba(255, 60, 60, 0.35)',
    color: '#ff8c8c',
});

export const agentStatusCard = style({
    display: 'flex',
    flexDirection: 'column',
    flexShrink: 0,
    backgroundColor: vars.color.panel,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    overflow: 'hidden',
});

export const cioStatusRow = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    padding: '10px 14px',
    backgroundColor: 'rgba(167, 139, 250, 0.07)',
    borderBottom: `1px solid ${vars.color.border}`,
});

export const agentGrid = style({
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
    gap: 8,
    padding: 10,
});

export const agentCard = style({
    display: 'flex',
    flexDirection: 'column',
    gap: 7,
    minWidth: 0,
    padding: '10px 12px',
    backgroundColor: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: 5,
});

export const agentHeader = style({
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 8,
});

export const agentTask = style({
    fontSize: '0.74rem',
    lineHeight: 1.4,
    color: vars.color.foreground,
});

export const agentMeta = style({
    marginTop: 2,
    fontSize: '0.68rem',
    lineHeight: 1.35,
    color: vars.color.mutedForeground,
    fontFamily: vars.font.mono,
});

const agentBadgeBase = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    flexShrink: 0,
    padding: '3px 7px',
    borderRadius: 4,
    fontSize: '0.68rem',
    fontWeight: 700,
} as const;

export const agentBadgeWorking = style({
    ...agentBadgeBase,
    color: '#8bd6a5',
    backgroundColor: 'rgba(22, 179, 137, 0.15)',
    border: '1px solid rgba(22, 179, 137, 0.35)',
});

export const agentBadgeStandby = style({
    ...agentBadgeBase,
    color: '#f6c453',
    backgroundColor: 'rgba(246, 196, 83, 0.12)',
    border: '1px solid rgba(246, 196, 83, 0.3)',
});

export const agentBadgeOffline = style({
    ...agentBadgeBase,
    color: '#8b94a7',
    backgroundColor: 'rgba(139, 148, 167, 0.1)',
    border: `1px solid ${vars.color.border}`,
});

export const agentEmpty = style({
    padding: 12,
    color: vars.color.mutedForeground,
    fontSize: '0.75rem',
});

