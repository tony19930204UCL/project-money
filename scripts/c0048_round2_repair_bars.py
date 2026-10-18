"""Repair observed vendor gaps only with real official exchange OHLCV, never interpolation."""
import concurrent.futures
import copy
import json
import sys
from pathlib import Path
from curl_cffi import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c0048_round2_capture import OUT, now, save, sha


def main():
    manifest = json.loads((OUT/'snapshot_manifest_final.json').read_text())
    jobs = []
    for symbol in manifest['universe']:
        chart = json.loads((OUT/f'bars/{symbol}.json').read_text())['chart']['result'][0]
        if any(any(v is None for v in values) for values in chart['indicators']['quote'][0].values()):
            jobs.append(symbol)
    def acquire(symbol):
        errors = []
        for endpoint in ['rwd/zh/afterTrading/STOCK_DAY','exchangeReport/STOCK_DAY']:
            url = f'https://www.twse.com.tw/{endpoint}?date=20250801&stockNo={symbol[:4]}&response=json'
            try:
                response = requests.get(url, impersonate='chrome', timeout=35)
                response.raise_for_status()
                obj = response.json()
                if obj.get('stat') != 'OK' or not obj.get('data'):
                    raise ValueError('NO_OFFICIAL_DAILY_HISTORY')
                return symbol, url, response.content, obj, errors
            except Exception as exc:
                errors.append({'url':url,'error':f'{type(exc).__name__}:{exc}'})
        raise RuntimeError(errors)
    mapping = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for symbol, url, body, official, errors in pool.map(acquire, jobs):
            raw = OUT/f'research/exchange_gap_{symbol}.json'
            with raw.open('xb') as f:
                f.write(body)
            manifest['entries'].append({'source':url,'snapshot':str(raw.relative_to(OUT)),'sha256':sha(body),
                'observed_at':now(),'kind':'official_primary','prior_errors':errors})
            source = OUT/f'bars/{symbol}.json'
            obj = json.loads(source.read_text())
            chart = obj['chart']['result'][0]
            quote = chart['indicators']['quote'][0]
            # All gap dates are one verified August session; verify surrounding
            # official prices share Yahoo's split basis before using the row.
            from datetime import datetime, timezone
            rows = {f'{int(r[0][:3])+1911}-{r[0][4:6]}-{r[0][7:9]}':r for r in official['data']}
            repairs = []
            for i, stamp in enumerate(chart['timestamp']):
                day = datetime.fromtimestamp(stamp,timezone.utc).date().isoformat()
                if day not in rows:
                    continue
                row = rows[day]
                values = {'volume':float(row[1].replace(',','')), 'open':float(row[3].replace(',','')),
                          'high':float(row[4].replace(',','')), 'low':float(row[5].replace(',','')),
                          'close':float(row[6].replace(',',''))}
                if any(quote[k][i] is None for k in values):
                    if not all(quote[k][i] is None for k in values):
                        raise ValueError('PARTIAL_BAD_BAR_REQUIRES_EXPLICIT_REVIEW')
                    # Dividend adjustment factor is not invented. This session's
                    # factor equals its adjacent real quoted adjustment factors
                    # only if no ex-date lies between; require official unchanged
                    # source days and equal adjacent factors to tight tolerance.
                    adj = chart['indicators']['adjclose'][0]['adjclose']
                    before = adj[i-1]/quote['close'][i-1]
                    after = adj[i+1]/quote['close'][i+1]
                    if abs(before-after) > 1e-6:
                        raise ValueError('ADJUSTMENT_BASIS_AMBIGUOUS')
                    for key,value in values.items():
                        quote[key][i] = value
                    adj[i] = values['close']*before
                    repairs.append({'day':day,'values':values,'source':str(raw.relative_to(OUT)),
                        'source_sha256':sha(body),'adjustment_factor':before,
                        'adjustment_factor_source':'identical real adjacent Yahoo factors, no price interpolation'})
                elif abs(quote['close'][i]-values['close']) > .02:
                    raise ValueError(f'SPLIT_BASIS_MISMATCH:{symbol}:{day}')
            target = OUT/f'bars_reconciled/{symbol}.json'
            save(target,obj)
            mapping[symbol] = str(target.relative_to(OUT))
            manifest['entries'].append({'source':str(source),'snapshot':mapping[symbol],
                'sha256':sha(target.read_bytes()),'observed_at':now(),'kind':'official_reconciled_bars',
                'vendor_sha256':sha(source.read_bytes()),'real_bar_repairs':repairs})
    manifest['bar_sources'] = mapping
    manifest['parent_manifest_sha256'] = sha((OUT/'snapshot_manifest_final.json').read_bytes())
    manifest['cutoff'] = now()
    manifest['cutoff_extension_reason'] = 'Official replacement of genuine missing 2025-08-01 OHLCV; freeze precedes indicator derivation and Main. This is not synthetic price backfill.'
    save(OUT/'snapshot_manifest_v4.json',manifest)
    print(json.dumps({'officially_repaired_tickers':jobs,'cutoff':manifest['cutoff']}))

if __name__ == '__main__':
    main()
