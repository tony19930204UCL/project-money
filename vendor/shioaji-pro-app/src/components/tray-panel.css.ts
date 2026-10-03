// src/components/tray-panel.css.ts

import { style, styleVariants } from '@vanilla-extract/css';
import { vars } from '../theme.css';

export const wrap = style({
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    background: vars.color.panel,
    border: `1px solid ${vars.color.borderBright}`,
    borderRadius: vars.radius.md,
    overflow: 'hidden',
});

export const header = style({
    display: 'flex',
    alignItems: 'center',
    gap: vars.space.sm,
    padding: `6px ${vars.space.md}`,
    borderBottom: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

export const title = style({
    fontFamily: vars.font.display,
    fontSize: '0.78rem',
    fontWeight: 700,
    color: vars.color.foreground,
});

export const headPnl = style({
    flex: 1,
    textAlign: 'right',
    fontFamily: vars.font.mono,
    fontSize: '0.74rem',
    fontWeight: 600,
    fontVariantNumeric: 'tabular-nums',
});

export const gearBtn = style({
    fontSize: '0.78rem',
    width: '24px',
    height: '24px',
    lineHeight: 1,
    cursor: 'pointer',
    background: 'transparent',
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    color: vars.color.mutedForeground,
    flexShrink: 0,
    ':hover': { color: vars.color.foreground },
});

export const gearRow = style({
    display: 'flex',
    gap: vars.space.xs,
    padding: `4px ${vars.space.md}`,
    borderBottom: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

const gearOptBase = style({
    flex: 1,
    fontFamily: vars.font.body,
    fontSize: '0.66rem',
    padding: '3px 0',
    cursor: 'pointer',
    borderRadius: vars.radius.sm,
    border: '1px solid',
});

export const gearOpt = styleVariants({
    on: [
        gearOptBase,
        {
            color: vars.color.accent,
            borderColor: vars.color.accent,
            background: vars.color.accentDim,
            fontWeight: 600,
        },
    ],
    off: [
        gearOptBase,
        {
            color: vars.color.mutedForeground,
            borderColor: vars.color.border,
            background: 'transparent',
        },
    ],
});

export const scroller = style({
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    padding: `2px 0 ${vars.space.sm}`,
});

export const sectionTitle = style({
    display: 'block',
    fontFamily: vars.font.display,
    fontSize: '0.6rem',
    fontWeight: 600,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: vars.color.mutedForeground,
    padding: `8px ${vars.space.md} 3px`,
});

export const row = style({
    display: 'grid',
    gridTemplateColumns: '3.6rem minmax(0,1fr) auto auto',
    alignItems: 'center',
    columnGap: vars.space.sm,
    width: '100%',
    padding: `3px ${vars.space.md}`,
    fontFamily: vars.font.mono,
    fontSize: '0.72rem',
    fontVariantNumeric: 'tabular-nums',
    textAlign: 'left',
    cursor: 'pointer',
    background: 'transparent',
    border: 'none',
    color: vars.color.foreground,
    ':hover': { background: vars.color.muted },
});

// with the sparkline filling the middle gap between name and price
export const rowSpark = style([
    row,
    {
        gridTemplateColumns: '3.3rem 4.6rem minmax(0,1fr) auto auto',
    },
]);

export const code = style({
    fontWeight: 600,
});

export const name = style({
    fontFamily: vars.font.body,
    fontSize: '0.66rem',
    color: vars.color.mutedForeground,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
});

export const num = style({
    textAlign: 'right',
    fontWeight: 600,
});

export const numSm = style({
    textAlign: 'right',
    fontSize: '0.66rem',
});

export const empty = style({
    display: 'block',
    padding: `4px ${vars.space.md}`,
    fontSize: '0.68rem',
    color: vars.color.mutedForeground,
});
