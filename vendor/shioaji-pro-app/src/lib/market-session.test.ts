// src/lib/market-session.test.ts
import { describe, expect, it } from 'vitest';
import { getTwSession, getUsSession } from './market-session';

describe('market-session calculations', () => {
    describe('TW session detection', () => {
        it('detects weekend as closed', () => {
            // 2026-09-27 is a Sunday
            const sunday = new Date('2026-09-27T10:00:00Z');
            const session = getTwSession(sunday);
            expect(session.status).toBe('WEEKEND');
            expect(session.isOpen).toBe(false);
            expect(session.label).toContain('休市');
        });

        it('detects regular open hours on weekday', () => {
            // 2026-09-28 is Monday, 09:30 TW time (01:30 UTC)
            const mondayOpen = new Date('2026-09-28T01:30:00Z');
            const session = getTwSession(mondayOpen);
            expect(session.status).toBe('OPEN');
            expect(session.isOpen).toBe(true);
            expect(session.label).toContain('台股常規');
        });

        it('detects pre-market on weekday', () => {
            // 08:45 TW time (00:45 UTC)
            const mondayPre = new Date('2026-09-28T00:45:00Z');
            const session = getTwSession(mondayPre);
            expect(session.status).toBe('PRE_MARKET');
            expect(session.isOpen).toBe(true);
            expect(session.label).toContain('試撮');
        });

        it('detects post-market pricing on weekday', () => {
            // 14:00 TW time (06:00 UTC)
            const mondayPost = new Date('2026-09-28T06:00:00Z');
            const session = getTwSession(mondayPost);
            expect(session.status).toBe('POST_MARKET');
            expect(session.isOpen).toBe(true);
            expect(session.label).toContain('盤後');
        });

        it('detects night session for futures', () => {
            // 20:00 TW time (12:00 UTC)
            const mondayNight = new Date('2026-09-28T12:00:00Z');
            const session = getTwSession(mondayNight);
            expect(session.status).toBe('NIGHT_SESSION');
            expect(session.isOpen).toBe(true);
            expect(session.label).toContain('夜盤');
        });
    });

    describe('US session detection', () => {
        it('detects weekend as closed', () => {
            const sunday = new Date('2026-09-27T15:00:00Z');
            const session = getUsSession(sunday);
            expect(session.status).toBe('WEEKEND');
            expect(session.isOpen).toBe(false);
        });

        it('detects regular trading hours on weekday', () => {
            // 11:00 AM ET (15:00 UTC during EDT)
            const weekdayOpen = new Date('2026-09-28T15:00:00Z');
            const session = getUsSession(weekdayOpen);
            expect(session.status).toBe('OPEN');
            expect(session.isOpen).toBe(true);
            expect(session.label).toContain('常規');
        });
    });
});
