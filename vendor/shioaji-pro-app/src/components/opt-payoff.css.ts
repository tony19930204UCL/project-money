// src/components/opt-payoff.css.ts

import { style } from '@vanilla-extract/css';
import { vars } from '../theme.css';

export const wrap = style({
    display: 'flex',
    flexDirection: 'column',
    minHeight: 0,
    height: '100%',
});

export const toolbar = style({
    display: 'flex',
    alignItems: 'center',
    gap: vars.space.sm,
    padding: `4px ${vars.space.sm}`,
    borderBottom: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

export const monthSelect = style({
    fontFamily: vars.font.mono,
    fontSize: '0.68rem',
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    padding: '2px 6px',
    outline: 'none',
});

export const warn = style({
    fontSize: '0.6rem',
    color: vars.color.amber,
});

export const chart = style({
    width: '100%',
    height: '220px',
    flexShrink: 0,
    display: 'block',
});

export const legList = style({
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    padding: `2px ${vars.space.sm}`,
    borderTop: `1px solid ${vars.color.border}`,
});

export const legRow = style({
    display: 'flex',
    alignItems: 'center',
    gap: vars.space.sm,
    padding: '2px 0',
    fontSize: '0.7rem',
    cursor: 'pointer',
});

export const legLabel = style({
    fontFamily: vars.font.mono,
    flex: 1,
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
});

export const legRemove = style({
    fontSize: '0.62rem',
    width: '18px',
    height: '18px',
    cursor: 'pointer',
    background: 'transparent',
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    color: vars.color.mutedForeground,
    ':hover': { color: vars.color.danger, borderColor: vars.color.danger },
});

export const simRow = style({
    display: 'flex',
    gap: vars.space.xs,
    padding: vars.space.sm,
    borderTop: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

export const addBtn = style({
    fontFamily: vars.font.display,
    fontSize: '0.66rem',
    fontWeight: 600,
    color: vars.color.accent,
    background: vars.color.accentDim,
    border: `1px solid ${vars.color.accent}`,
    borderRadius: vars.radius.sm,
    padding: '2px 10px',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
});
