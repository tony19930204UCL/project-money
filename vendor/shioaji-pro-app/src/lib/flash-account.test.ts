import { describe, expect, it } from 'vitest';
import { accountMatches, scopedFlashRows } from './flash-account';
import type { Account } from './types/portfolio';
const a: Account = { account_type: 'F', broker_id: 'B', account_id: 'A', signed: true, person_id: '', username: '' };
const b = { ...a, account_id: 'B' };
describe('flash account ownership', () => {
    it('isolates same-product accounts and rejects missing or conflicting ownership', () => {
        const mine = { code: 'TMF', account: a };
        const other = { code: 'TMF', account: b };
        const conflict = { code: 'TMF', account: a, order: { account: b } };
        expect(scopedFlashRows([mine, other, conflict, { code: 'TMF', account: undefined }], a)).toEqual([mine]);
        expect(scopedFlashRows([mine], undefined)).toEqual([]);
        expect(scopedFlashRows([{ order: { account: a } }], a)).toHaveLength(1);
    });
    it('compares market and broker as well as account id', () => {
        expect(accountMatches(a, { ...a, broker_id: 'OTHER' })).toBe(false);
        expect(accountMatches(a, { ...a, account_type: 'S' })).toBe(false);
        expect(accountMatches(a, null)).toBe(false);
    });
});
