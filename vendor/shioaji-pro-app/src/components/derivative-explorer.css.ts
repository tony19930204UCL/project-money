import { style, styleVariants } from '@vanilla-extract/css';
import { vars } from '../theme.css';

export const wrap = style({
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    minHeight: 0,
});

export const toolbar = style({
    display: 'flex',
    alignItems: 'center',
    gap: vars.space.xs,
    padding: `5px ${vars.space.sm}`,
    borderBottom: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

export const picker = style({
    position: 'relative',
    flex: 1,
    minWidth: '8rem',
});

export const input = style({
    width: '100%',
    height: '26px',
    padding: '3px 8px',
    fontFamily: vars.font.body,
    fontSize: '0.7rem',
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    outline: 'none',
    '::placeholder': { color: vars.color.mutedForeground },
    ':focus': { borderColor: vars.color.accent },
});

export const suggestions = style({
    position: 'absolute',
    zIndex: 40,
    top: 'calc(100% + 3px)',
    left: 0,
    right: 0,
    maxHeight: '190px',
    overflowY: 'auto',
    background: vars.color.panelRaised,
    border: `1px solid ${vars.color.borderBright}`,
    borderRadius: vars.radius.sm,
    boxShadow: '0 8px 20px rgba(0, 0, 0, 0.28)',
});

export const suggestion = style({
    display: 'grid',
    gridTemplateColumns: '3.5rem minmax(0, 1fr)',
    gap: vars.space.sm,
    width: '100%',
    padding: '5px 8px',
    textAlign: 'left',
    color: vars.color.foreground,
    background: 'transparent',
    border: 0,
    borderBottom: `1px solid ${vars.color.border}`,
    cursor: 'pointer',
    ':hover': { background: vars.color.muted },
});

export const suggestionCode = style({
    fontFamily: vars.font.mono,
    fontSize: '0.68rem',
    fontWeight: 600,
});

export const suggestionName = style({
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    fontSize: '0.68rem',
    color: vars.color.mutedForeground,
});

const segmentBase = style({
    height: '26px',
    padding: '2px 8px',
    fontFamily: vars.font.body,
    fontSize: '0.65rem',
    whiteSpace: 'nowrap',
    cursor: 'pointer',
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
});

export const segment = styleVariants({
    off: [
        segmentBase,
        {
            color: vars.color.mutedForeground,
            background: 'transparent',
            ':hover': { color: vars.color.foreground },
        },
    ],
    on: [
        segmentBase,
        {
            color: vars.color.foreground,
            background: vars.color.muted,
            borderColor: vars.color.borderBright,
        },
    ],
});

export const select = style({
    height: '26px',
    padding: '2px 6px',
    fontFamily: vars.font.body,
    fontSize: '0.65rem',
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    outline: 'none',
});

export const summary = style({
    display: 'flex',
    alignItems: 'center',
    gap: vars.space.md,
    minHeight: '30px',
    padding: `4px ${vars.space.sm}`,
    color: vars.color.mutedForeground,
    fontSize: '0.65rem',
    borderBottom: `1px solid ${vars.color.border}`,
    flexShrink: 0,
});

export const summaryStrong = style({
    color: vars.color.foreground,
    fontFamily: vars.font.mono,
    fontWeight: 600,
});

export const scroll = style({
    minHeight: 0,
    flex: 1,
    overflow: 'auto',
});

export const table = style({
    width: '100%',
    borderCollapse: 'collapse',
    tableLayout: 'fixed',
    fontFamily: vars.font.mono,
    fontSize: '0.67rem',
    fontVariantNumeric: 'tabular-nums',
});

export const th = style({
    position: 'sticky',
    top: 0,
    zIndex: 2,
    padding: '4px 6px',
    textAlign: 'right',
    fontFamily: vars.font.body,
    fontSize: '0.6rem',
    fontWeight: 500,
    color: vars.color.mutedForeground,
    background: vars.color.panel,
    borderBottom: `1px solid ${vars.color.border}`,
});

export const thLeft = style([th, { textAlign: 'left' }]);

export const row = style({
    cursor: 'pointer',
    borderBottom: `1px solid ${vars.color.border}`,
    ':hover': { background: vars.color.muted },
});

export const td = style({
    padding: '5px 6px',
    textAlign: 'right',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
});

export const tdLeft = style([td, { textAlign: 'left' }]);

export const contractName = style({
    display: 'block',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    color: vars.color.mutedForeground,
    fontFamily: vars.font.body,
    fontSize: '0.61rem',
});

export const badge = style({
    display: 'inline-block',
    padding: '1px 4px',
    color: vars.color.accent,
    background: vars.color.accentDim,
    borderRadius: vars.radius.sm,
    fontFamily: vars.font.body,
    fontSize: '0.58rem',
});

export const iconButton = style({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '24px',
    height: '24px',
    padding: 0,
    color: vars.color.mutedForeground,
    background: 'transparent',
    border: `1px solid transparent`,
    borderRadius: vars.radius.sm,
    cursor: 'pointer',
    ':hover': {
        color: vars.color.amber,
        background: vars.color.muted,
        borderColor: vars.color.border,
    },
});

export const empty = style({
    display: 'grid',
    placeItems: 'center',
    minHeight: '8rem',
    padding: vars.space.md,
    color: vars.color.mutedForeground,
    fontSize: '0.7rem',
});

export const error = style([empty, { color: vars.color.danger }]);

