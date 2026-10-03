import { ACTIVE_ORDER_STATUSES, type Trade } from './types/order';

/** 委託顯示用剩餘量；減量累計在 cancel_quantity，不會改寫原始 order.quantity。 */
export function remainingWorkingOrderQuantity(trade: Trade): number {
    if (!ACTIVE_ORDER_STATUSES.has(trade.status.status)) return 0;
    const { quantity } = trade.order;
    const { deal_quantity, cancel_quantity } = trade.status;
    if (![quantity, deal_quantity, cancel_quantity].every(n => Number.isFinite(n) && n >= 0)) return 0;
    return Math.max(0, quantity - deal_quantity - cancel_quantity);
}
