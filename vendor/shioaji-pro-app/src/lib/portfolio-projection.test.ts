import { describe, expect, it } from 'vitest';
import { normalizeOrderEvent } from './order-report';
import { applyPositionFill, markPosition, positionFill, type PositionFill } from './portfolio-projection';
import type { Account, AccountedPosition } from './types/portfolio';

const account: Account = { account_type: 'S', broker_id: 'test', account_id: 'a', person_id: '', signed: true, username: '' };
const base: AccountedPosition = { id: 1, code: '2330', direction: 'Buy', quantity: 1000, price: 100, last_price: 110, pnl: 9900, yd_quantity: 1000, cond: 'Cash', account };
const fill: PositionFill = { key: 'fill', tradeId: 'order', account, code: '2330', action: 'Buy', quantity: 100, price: 120, ts: 1789000000, condition: 'Cash', openClose: '' };

describe('position projection', () => {
    it('marks current value without discarding broker baseline adjustments', () => {
        expect(markPosition(base, 112, 1)).toMatchObject({ last_price: 112, pnl: 11900 });
        expect(markPosition({ ...base, direction: 'Sell' }, 112, 1).pnl).toBe(7900);
    });
    it('uses explicit futures multiplier and ignores invalid quotes', () => {
        expect(markPosition({ ...base, quantity: 2 }, 111, 200).pnl).toBe(10300);
        expect(markPosition(base, NaN, 1)).toBe(base);
        expect(markPosition(base, 0, 1)).toBe(base);
        expect(markPosition(base, 112, 0)).toBe(base);
    });
    it('adds shares at weighted cost and retains existing mark-to-market', () => {
        const p = applyPositionFill([base], fill, 1)![0]!;
        expect(p.quantity).toBe(1100);
        expect(p.price).toBeCloseTo(101.8181818);
        expect(p.pnl).toBe(19900);
        expect(p.last_price).toBe(120);
        expect('yd_quantity' in p && p.yd_quantity).toBe(1000);
    });
    it('reduces or removes only the matching account holding', () => {
        const other = { ...base, account: { ...account, account_id: 'b' } };
        expect(applyPositionFill([base, other], { ...fill, action: 'Sell', quantity: 1000 }, 1)).toEqual([other]);
        expect(applyPositionFill([base], { ...fill, action: 'Sell', quantity: 100 }, 1)![0]!.quantity).toBe(900);
    });
    it('does not invent short cash holdings or choose among ambiguous lots', () => {
        expect(applyPositionFill([base], { ...fill, action: 'Sell', quantity: 1001 }, 1)).toBeNull();
        expect(applyPositionFill([base, { ...base, id: 2 }], fill, 1)).toBeNull();
    });
    it('handles explicit new/cover and net auto futures without guessing hedged auto', () => {
        const a = { ...account, account_type: 'F' };
        const p = { ...base, account: a, quantity: 2 };
        const f = { ...fill, account: a, action: 'Sell' as const, quantity: 3, openClose: 'Auto' };
        expect(applyPositionFill([p], f, 200)).toEqual([expect.objectContaining({ direction: 'Sell', quantity: 1, price: 120 })]);
        expect(applyPositionFill([p], { ...f, openClose: 'Cover' }, 200)).toBeNull();
        expect(applyPositionFill([p], { ...f, openClose: 'New' }, 200)).toHaveLength(2);
        expect(applyPositionFill([p, { ...p, direction: 'Sell' }], f, 200)).toBeNull();
    });
    it.each(['Buy', 'Sell'] as const)('keeps weighted futures cost and proportional unrealized PnL through %s New and Cover', direction => {
        const owner: Account = { ...account, account_type: 'F' };
        const sign = direction === 'Buy' ? 1 : -1;
        const holding: AccountedPosition = { id: 10, code: 'TXFI6', account: owner,
            direction, quantity: 2, price: 100, last_price: 110, pnl: sign * 4000 };
        const other: AccountedPosition = { ...holding, id: 11, account: { ...owner, account_id: 'other' } };
        const addition: PositionFill = { ...fill, account: owner, code: holding.code,
            action: direction, quantity: 2, price: 120, condition: '', openClose: 'New' };
        const added = applyPositionFill([holding, other], addition, 200)!;
        // Four contracts at average cost 110; at 120 the total unrealized PnL is ±8000.
        expect(added[0]).toMatchObject({ quantity: 4, price: 110, last_price: 120, pnl: sign * 8000 });
        expect(added[1]).toBe(other);
        const marked = markPosition(added[0]!, 125, 200);
        expect(marked.pnl).toBe(sign * 12000);
        const cover: PositionFill = { ...addition, key: 'cover-1', tradeId: 'cover',
            action: direction === 'Buy' ? 'Sell' : 'Buy', quantity: 1, price: 125, openClose: 'Cover' };
        const partial = applyPositionFill([marked, other], cover, 200)!;
        expect(partial[0]).toMatchObject({ direction, quantity: 3, price: 110, last_price: 125, pnl: sign * 9000 });
        expect(partial[1]).toBe(other);
        const moved = markPosition(partial[0]!, 123, 200);
        expect(moved.pnl).toBe(sign * 7800);
        const closed = applyPositionFill([moved, other], { ...cover, key: 'cover-rest', quantity: 3, price: 123 }, 200)!;
        expect(closed).toEqual([other]);
        expect(closed[0]).toBe(other);
    });
    it('retains stock average cost and remaining unrealized PnL after partial and full sale', () => {
        // Synthetic unit data: excludes fees and realized PnL, which are not computed here.
        const first = applyPositionFill([], { ...fill, quantity: 1000, price: 412.5 }, 1)![0]!;
        const added = applyPositionFill([first], { ...fill, key: 'buy-2', quantity: 1000, price: 411 }, 1)![0]!;
        expect(added).toMatchObject({ quantity: 2000, price: 411.75, last_price: 411, pnl: -1500 });
        const marked = markPosition(added, 409.5, 1);
        expect(marked.pnl).toBe(-4500);
        const other = { ...marked, account: { ...account, account_id: 'other' } };
        const sell: PositionFill = { ...fill, key: 'sell-1', action: 'Sell', quantity: 1000, price: 409.5 };
        const partial = applyPositionFill([marked, other], sell, 1)!;
        expect(partial[0]).toMatchObject({ quantity: 1000, price: 411.75, last_price: 409.5, pnl: -2250 });
        expect(partial[1]).toBe(other);
        const moved = markPosition(partial[0]!, 410, 1);
        expect(moved.pnl).toBe(-1750);
        const closed = applyPositionFill([moved, other], { ...sell, key: 'sell-rest', price: 410 }, 1)!;
        expect(closed).toEqual([other]);
        expect(closed[0]).toBe(other);
    });
    it('requires account and fill identity; normalizes lots to shares', () => {
        const body = { trade_id: 'order', exchange_seq: 'fill-1', broker_id: 'test', account_id: 'a', code: '2330', action: 'Buy', price: 120, quantity: 1, order_lot: 'Common', order_cond: 'Cash', ts: fill.ts };
        const parse = (b: object) => positionFill(normalizeOrderEvent({ state: 'StockDeal', data: { StockDeal: b } })!, [account], []);
        expect(parse(body)?.quantity).toBe(1000);
        expect(parse({ ...body, order_lot: 'IntradayOdd' })?.quantity).toBe(1);
        expect(parse({ ...body, account_id: '' })).toBeNull();
        expect(parse({ ...body, exchange_seq: '' })).toBeNull();
        expect(parse({ ...body, order_cond: 'MarginTrading' })).toBeNull();
        expect(parse({ ...body, order_cond: 'Netting' })).toBeNull();
        expect(parse({ ...body, quantity: 0 })).toBeNull();
    });
});

it('projects native Common Buy1 as 1000 shares while retaining the stock order lot', async () => {
    const { default: rawEvents } = await import('./fixtures/native-simulation-stock-common-1.7.5.json');
    const { projectOrderReport, projectTradeDeal } = await import('./order-projection');
    const fixtureAccount = {...account, broker_id:'fixture', account_id:'fixture'};
    const reports = rawEvents.map(normalizeOrderEvent);
    const newBuy = reports.find(r => r?.kind === 'order' && r.opType === 'New' && r.action === 'Buy')!;
    const buyDeal = reports.find(r => r?.kind === 'deal' && r.action === 'Buy')!;
    expect(newBuy).toMatchObject({orderLot:'Common',quantity:1,priceType:'MKT',orderType:'IOC'});
    expect(buyDeal).toMatchObject({orderLot:'Common',quantity:1,price:424});
    const trades = projectOrderReport([], newBuy, [fixtureAccount])!;
    expect(projectTradeDeal(trades,buyDeal)![0]!.status).toMatchObject({status:'Filled',deal_quantity:1});
    const projectedFill = positionFill(buyDeal,[fixtureAccount],trades)!;
    expect(projectedFill.quantity).toBe(1000);
    expect(applyPositionFill([],projectedFill,1)![0]).toMatchObject({quantity:1000,price:424});
    const exitOrder = reports.find(r => r?.kind === 'order' && r.opType === 'New' && r.action === 'Sell')!;
    // Native Flash flatten captured the 2000-share holding, despite quantity input 1.
    // This checks the request report only; it does not claim a Sell fill or Odd QA.
    expect(exitOrder).toMatchObject({orderLot:'Common',quantity:2});
});

it('replays the native two-part Common Sell fill from an explicit 2000-share baseline', async () => {
    const { default: rawEvents } = await import('./fixtures/native-simulation-stock-common-1.7.5.json');
    const { projectOrderReport, projectTradeDeal } = await import('./order-projection');
    const fixtureAccount = {...account, broker_id:'fixture', account_id:'fixture'};
    const reports = rawEvents.map(normalizeOrderEvent);
    const sellOrder = reports.find(r => r?.kind === 'order' && r.action === 'Sell')!;
    const sells = reports.filter(r => r?.kind === 'deal' && r.action === 'Sell');
    expect(sells).toHaveLength(2);
    let trades = projectOrderReport([],sellOrder,[fixtureAccount])!;
    // First Buy was not captured; this is an explicit pre-Sell holding baseline,
    // not a fabricated wire event or a claim that the fixture contains both Buys.
    let positions: AccountedPosition[] = [{...base,account:fixtureAccount,code:sellOrder.code,quantity:2000,price:424,last_price:424,pnl:0,yd_quantity:0}];
    for (const [index,report] of sells.entries()) {
        expect(report).toMatchObject({quantity:1,orderLot:'Common',price:424});
        const next = projectTradeDeal(trades,report!)!;
        expect(next[0]!.status).toMatchObject({status:index===0?'PartFilled':'Filled',deal_quantity:index+1});
        expect(projectTradeDeal(next,report!)).toBe(next);
        const projected = positionFill(report!,[fixtureAccount],trades)!;
        positions = applyPositionFill(positions,projected,1)!;
        expect(positions.reduce((sum,p)=>sum+p.quantity,0)).toBe(index===0?1000:0);
        trades=next;
    }
    expect(positions).toEqual([]);
});
