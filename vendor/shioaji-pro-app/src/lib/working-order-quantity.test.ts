import { expect, it } from 'vitest';
import nativeMutations from './fixtures/native-simulation-mutations-1.7.5.json';
import type { Trade } from './types/order';
import { remainingWorkingOrderQuantity } from './working-order-quantity';

function trade(quantity: number, dealt: number, cancelled: number, status: Trade['status']['status'] = 'Submitted'): Trade {
    return { order: { quantity }, status: { status, deal_quantity: dealt, cancel_quantity: cancelled } } as Trade;
}

it('shows one remaining after the native simulation reduces an original two-unit order', () => {
    const newOrder = nativeMutations[0]!.data.FuturesOrder!;
    const reduced = nativeMutations[2]!.data.FuturesOrder!;
    expect(newOrder.order.quantity).toBe(2);
    expect(reduced.status.cancel_quantity).toBe(1);
    expect(remainingWorkingOrderQuantity(trade(newOrder.order.quantity, 0, reduced.status.cancel_quantity))).toBe(1);
});

it('subtracts both cumulative reductions and partial fills from the original quantity', () => {
    expect(remainingWorkingOrderQuantity(trade(5, 1, 2, 'PartFilled'))).toBe(2);
    expect(remainingWorkingOrderQuantity(trade(5, 1, 4, 'PartFilled'))).toBe(0);
});

it.each(['Cancelled', 'Filled', 'Failed', 'Inactive'] as const)('hides terminal %s orders even if their quantities are incomplete', status => {
    expect(remainingWorkingOrderQuantity(trade(2, 0, 0, status))).toBe(0);
});

it('does not render a negative or invalid working quantity', () => {
    expect(remainingWorkingOrderQuantity(trade(2, 1, 2))).toBe(0);
    expect(remainingWorkingOrderQuantity(trade(2, 0, Number.NaN))).toBe(0);
});

it('keeps contradictory native HTTP Submitted responses raw but excludes fully accounted quantities', async () => {
    const { default: rows } = await import('./fixtures/native-simulation-zero-working-trades-1.7.5.json');
    expect(rows.length).toBeGreaterThanOrEqual(2);
    for (const row of rows) {
        expect(row.status.status).toBe('Submitted');
        expect(remainingWorkingOrderQuantity(row as Trade)).toBe(0);
        expect(row.status.status).toBe('Submitted');
    }
});
