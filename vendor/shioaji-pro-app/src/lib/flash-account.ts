import type { Account } from './types/portfolio';

type AccountIdentity = Pick<Account, 'account_type' | 'broker_id' | 'account_id'>;
export function accountMatches(a: AccountIdentity | null | undefined, b: AccountIdentity | null | undefined): boolean {
    return !!a && !!b && !!a.broker_id && !!a.account_id && a.account_type === b.account_type
        && a.broker_id === b.broker_id && a.account_id === b.account_id;
}

/** Unknown ownership is never inferred from the currently selected account. */
export function scopedFlashRows<T extends { account?: AccountIdentity; order?: { account?: AccountIdentity } }>(rows: T[], account?: Account): T[] {
    return rows.filter(row => accountMatches(row.account ?? row.order?.account, account)
        && (!row.account || !row.order?.account || accountMatches(row.account, row.order.account)));
}
