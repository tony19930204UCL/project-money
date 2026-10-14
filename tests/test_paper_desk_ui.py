"""Executable JavaScript DOM behavior checks for the paper desk (Node.js required)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin/ui/paper-desk.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for executable UI harness")
def test_paper_desk_fetch_render_interactions_and_failure():
    harness = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(id){this.id=id;this.value='ALL';this.dataset={};this.listeners={};this.innerHTML='';this.textContent='';this.disabled=false;this.attrs={};this.classList={toggle:()=>{}};}
 addEventListener(n,f){this.listeners[n]=f}
 setAttribute(k,v){this.attrs[k]=v}
 querySelectorAll(q){if(q==='[data-sort]'){const keys=[...this.innerHTML.matchAll(/data-sort="([^"]+)"/g)].map(m=>m[1]);this.sortButtons=this.sortButtons||{};return keys.map(k=>{if(!this.sortButtons[k]){this.sortButtons[k]=new Element();this.sortButtons[k].dataset.sort=k}return this.sortButtons[k]})}return []}
 click(){this.listeners.click()}
 change(){this.listeners.change()}
}
const ids={};for(const id of ['overview','results','accounting','feed','notice','updated','refresh','market','product','strategy'])ids[id]=new Element(id);
const tabs=['open','closed','fills'].map(t=>{let e=new Element();e.dataset.tab=t;return e});
const document={getElementById:id=>ids[id],querySelectorAll:q=>q==='[data-tab]'?tabs:[]};
let pending=[],calls=[];
const payload={markets:{US:{state:'OK',equity:1000,cash:800,positions:[{ticker:'ZZZ',market:'US',qty:2,horizon:'swing',product:'spot',strategy_id:'momentum',unrealized_net:null},{ticker:'AAA',market:'US',qty:1,horizon:'day',product:'spot',strategy_id:'meanrev',unrealized_net:5}]},TW:{state:'OK',positions:[]}},closed_trades:[{ticker:'CLOSED',market:'US',horizon:'swing',net_pnl:null,cost_status:'UNKNOWN_COST'}],fills:[{ticker:'FILL',market:'US',side:'BUY',size:1}],read_model:{position_reconciliation:{'US:ZZZ':{status:'JOURNAL_ACCOUNT_CONFLICT',account_qty:2,journal_reconstructed_open_qty:3,quarantine_reason:'orphan_ledger_position'}},realized_by_currency:{USD:null},realized_completeness:{USD:{verified_subtotal:12,complete:false,unknown_cost_trades:1}},feed_freshness:'UNKNOWN'}};
const fetch=(url,opts)=>{calls.push(url);return new Promise((resolve,reject)=>pending.push({resolve,reject}))};
const ctx={document,fetch,setInterval:()=>{},Date,Number,String,Object,Array,Error};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
async function flush(){await new Promise(resolve=>setImmediate(resolve))}
(async()=>{
assert.equal(calls[0],'/api/paper/desk');assert.match(ids.notice.textContent,/Loading/);
pending.shift().resolve({ok:true,json:async()=>payload});await flush();
assert.match(ids.results.innerHTML,/ZZZ/);assert.match(ids.results.innerHTML,/Unknown/);
assert.match(ids.accounting.innerHTML,/RECONCILIATION WARNING/);assert.match(ids.accounting.innerHTML,/verified subtotal/);assert.match(ids.accounting.innerHTML,/Unknown \/ incomplete/);assert.equal(ids.refresh.disabled,false);
ids.market.value='TW';ids.market.change();assert.doesNotMatch(ids.results.innerHTML,/<td>ZZZ<\/td>/);
ids.market.value='ALL';ids.market.change();
ids.strategy.value='swing';ids.strategy.change();assert.match(ids.results.innerHTML,/ZZZ/);assert.doesNotMatch(ids.results.innerHTML,/AAA<\\/td>/);ids.strategy.value='ALL';ids.strategy.change();
ids.product.value='future';ids.product.change();assert.match(ids.results.innerHTML,/No matching/);ids.product.value='ALL';ids.product.change();
let sortButton=ids.results.querySelectorAll('[data-sort]').find(x=>x.dataset.sort==='ticker');assert(ids.results.innerHTML.indexOf('AAA')<ids.results.innerHTML.indexOf('ZZZ'));sortButton.click();assert(ids.results.innerHTML.indexOf('ZZZ')<ids.results.innerHTML.indexOf('AAA'));sortButton.click();assert(ids.results.innerHTML.indexOf('AAA')<ids.results.innerHTML.indexOf('ZZZ'));
tabs[1].click();assert.match(ids.results.innerHTML,/CLOSED/);assert.match(ids.results.innerHTML,/UNKNOWN_COST/);
tabs[2].click();assert.match(ids.results.innerHTML,/FILL/);
ids.refresh.click();pending.shift().reject(Error('offline'));await flush();assert.match(ids.notice.textContent,/offline/);assert.match(ids.results.innerHTML,/FILL/);
ids.refresh.click();pending.shift().resolve({ok:false,status:503});await flush();assert.match(ids.notice.textContent,/HTTP 503/);
assert.equal(ids.refresh.disabled,false);console.log('DOM behavior PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(SCRIPT)], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DOM behavior PASS" in result.stdout
