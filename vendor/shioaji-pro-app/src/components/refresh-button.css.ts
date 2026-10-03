import { keyframes, style } from '@vanilla-extract/css';
import { vars } from '../theme.css';

export const refreshButton = style({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 28,
    height: 28,
    flexShrink: 0,
    marginLeft: 'auto',
    padding: 0,
    border: 'none',
    borderRadius: vars.radius.sm,
    background: 'transparent',
    color: vars.color.mutedForeground,
    cursor: 'pointer',
    ':hover': { color: vars.color.foreground, background: vars.color.muted },
    ':focus-visible': { outline: `2px solid ${vars.color.accent}`, outlineOffset: 1 },
    ':disabled': { opacity: 0.45, cursor: 'wait' },
});

const refreshRotation = keyframes({ to: { transform: 'rotate(360deg)' } });
export const refreshSpinning = style({
    animation: `${refreshRotation} 0.8s linear infinite`,
    '@media': { '(prefers-reduced-motion: reduce)': { animation: 'none' } },
});

