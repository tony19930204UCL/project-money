// src/components/custom-indicator-editor.css.ts — 自訂指標編輯器
// （metadata / 參數表 / 程式碼 / 驗證輸出）。

import { style, styleVariants } from '@vanilla-extract/css';
import { vars } from '../theme.css';

export const overlay = style({
    position: 'fixed',
    inset: 0,
    zIndex: 2100, // 蓋在指標選擇器之上
    background: 'rgba(0, 0, 0, 0.45)',
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'center',
    paddingTop: '6vh',
});

export const dialog = style({
    display: 'flex',
    flexDirection: 'column',
    width: 'min(48rem, 94vw)',
    maxHeight: '86vh',
    background: vars.color.panelRaised,
    border: `1px solid ${vars.color.borderBright}`,
    borderRadius: vars.radius.lg,
    boxShadow: '0 24px 64px rgba(0, 0, 0, 0.5)',
    overflow: 'hidden',
});

export const body = style({
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    padding: `0 ${vars.space.lg} ${vars.space.md}`,
    display: 'flex',
    flexDirection: 'column',
    gap: '10px',
});

export const metaRow = style({
    display: 'flex',
    gap: '10px',
    alignItems: 'flex-end',
});

export const field = style({
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    minWidth: 0,
});

export const fieldLabel = style({
    fontFamily: vars.font.display,
    fontSize: '0.62rem',
    fontWeight: 600,
    letterSpacing: '0.06em',
    color: vars.color.mutedForeground,
    userSelect: 'none',
});

export const textInput = style({
    fontFamily: vars.font.body,
    fontSize: '0.8rem',
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    padding: '6px 8px',
    outline: 'none',
    minWidth: 0,
    ':focus': { borderColor: vars.color.accent },
});

export const catBtnRow = style({
    display: 'flex',
    gap: '4px',
});

const catBtnBase = style({
    fontFamily: vars.font.body,
    fontSize: '0.74rem',
    cursor: 'pointer',
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    color: vars.color.mutedForeground,
    padding: '6px 10px',
    whiteSpace: 'nowrap',
    ':hover': { color: vars.color.foreground },
});

export const catBtn = styleVariants({
    normal: [catBtnBase],
    active: [
        catBtnBase,
        {
            borderColor: vars.color.accent,
            color: vars.color.foreground,
            fontWeight: 600,
        },
    ],
});

export const sectionTitle = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    fontFamily: vars.font.display,
    fontSize: '0.62rem',
    fontWeight: 600,
    letterSpacing: '0.08em',
    color: vars.color.mutedForeground,
    marginTop: '4px',
    userSelect: 'none',
});

export const paramRow = style({
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 5rem 5rem 5rem 5rem 1.6rem',
    gap: '6px',
    alignItems: 'center',
});

export const paramHead = style([
    paramRow,
    {
        fontFamily: vars.font.display,
        fontSize: '0.58rem',
        color: vars.color.mutedForeground,
        userSelect: 'none',
    },
]);

export const monoInput = style([
    textInput,
    { fontFamily: vars.font.mono, fontSize: '0.76rem' },
]);

export const smallBtn = style({
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    fontFamily: vars.font.body,
    fontSize: '0.72rem',
    cursor: 'pointer',
    background: 'transparent',
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    color: vars.color.mutedForeground,
    padding: '3px 8px',
    ':hover': { color: vars.color.foreground, borderColor: vars.color.borderBright },
});

export const iconBtn = style({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    cursor: 'pointer',
    background: 'transparent',
    border: 'none',
    color: vars.color.mutedForeground,
    padding: '4px',
    borderRadius: vars.radius.sm,
    ':hover': { color: vars.color.danger, background: vars.color.muted },
});

export const codeArea = style({
    fontFamily: vars.font.mono,
    fontSize: '0.76rem',
    lineHeight: 1.55,
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    padding: '10px 12px',
    outline: 'none',
    resize: 'vertical',
    minHeight: '13rem',
    whiteSpace: 'pre',
    tabSize: 4,
    ':focus': { borderColor: vars.color.accent },
});

export const helpBox = style({
    fontFamily: vars.font.mono,
    fontSize: '0.7rem',
    lineHeight: 1.7,
    color: vars.color.mutedForeground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    padding: '10px 12px',
    whiteSpace: 'pre-wrap',
    userSelect: 'text',
});

export const helpKey = style({
    color: vars.color.foreground,
});

export const errorBox = style({
    fontFamily: vars.font.mono,
    fontSize: '0.74rem',
    color: vars.color.danger,
    background: 'rgba(255, 77, 106, 0.08)',
    border: '1px solid rgba(255, 77, 106, 0.35)',
    borderRadius: vars.radius.md,
    padding: '8px 12px',
    whiteSpace: 'pre-wrap',
});

export const okNote = style({
    fontFamily: vars.font.body,
    fontSize: '0.74rem',
    color: vars.color.success,
});

export const outputRow = style({
    display: 'grid',
    gridTemplateColumns: '1.4rem 1fr 1fr 7rem',
    gap: '6px',
    alignItems: 'center',
});

export const swatchBtn = style({
    width: '18px',
    height: '18px',
    borderRadius: '4px',
    border: `1px solid ${vars.color.borderBright}`,
    cursor: 'pointer',
    padding: 0,
});

export const swatchPop = style({
    display: 'flex',
    flexWrap: 'wrap',
    gap: '4px',
    padding: '8px',
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.md,
    alignItems: 'center',
});

export const swatch = style({
    width: '16px',
    height: '16px',
    borderRadius: '3px',
    border: '1px solid rgba(255,255,255,0.12)',
    cursor: 'pointer',
    padding: 0,
    ':hover': { outline: `2px solid ${vars.color.accent}` },
});

export const hexInput = style([
    monoInput,
    { width: '6.2rem', padding: '3px 6px' },
]);

export const select = style({
    fontFamily: vars.font.body,
    fontSize: '0.74rem',
    color: vars.color.foreground,
    background: vars.color.inset,
    border: `1px solid ${vars.color.border}`,
    borderRadius: vars.radius.sm,
    padding: '5px 6px',
    outline: 'none',
    ':focus': { borderColor: vars.color.accent },
});

export const footer = style({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '8px',
    padding: `${vars.space.md} ${vars.space.lg}`,
    borderTop: `1px solid ${vars.color.border}`,
});

export const footerActions = style({
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
});
