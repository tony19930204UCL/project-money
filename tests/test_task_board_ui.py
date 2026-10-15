"""Execute real browser script with a minimal Node DOM/fetch environment."""
import shutil
import subprocess
import unittest
from pathlib import Path

JS=Path(__file__).resolve().parents[1]/"cio_market_lab/observability/task-board.js"
HARNESS=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.text='';this.listeners={};this.attributes={}}
 set textContent(v){this.text=String(v);this.children=[]}
 get textContent(){return this.text+this.children.map(c=>c.textContent).join('')}
 appendChild(n){this.children.push(n);return n}
 removeChild(n){this.children.splice(this.children.indexOf(n),1)}
 get firstChild(){return this.children[0]}
 addEventListener(k,f){this.listeners[k]=f}
 getAttribute(k){return this.attributes[k]}
}
const ids={};for(const id of ['summary','updated','errors','search','status','project','content'])ids[id]=new Node();
const buttons=['tasks','roadmap','changes'].map(v=>{const n=new Node('button');n.attributes['data-view']=v;return n});
global.Option=function(t,v){const n=new Node('option');n.textContent=t;n.value=v;return n};
global.document={getElementById:id=>ids[id],createElement:tag=>new Node(tag),querySelectorAll:()=>buttons};
let result={tasks:[{id:'task1',title:'<img onerror=alert(1)>',project:'P',lifecycle:'PLANNED',executor_kind:'manual',executor_health:'UNVERIFIED',dependencies:[]},{id:'task2',title:'阻塞工作',lifecycle:'BLOCKED',blocker:'等待',dependencies:[]},{id:'task3',title:'來源已接受',lifecycle:'DONE',acceptance_scope:'source_accepted',awaiting_acceptance:true,dependencies:[]}],projects:[{title:'P',stages:[{title:'驗收',observed_state:'original_host_pending',task_ids:['task3']}]}],diagnostics:[],polled_at:'2026-10-15'};
global.fetch=async()=>({ok:true,json:async()=>result});global.setInterval=()=>{};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
setImmediate(async()=>{
 await new Promise(r=>setImmediate(r));
 assert(ids.content.textContent.includes('<img onerror=alert(1)>'));
 assert(!ids.content.children.some(c=>c.tag==='img'));
 assert(ids.content.textContent.includes('來源已接受'));
 ids.search.value='task2';ids.search.listeners.input();assert(ids.content.textContent.includes('阻塞工作'));assert(!ids.content.textContent.includes('來源已接受'));
 ids.search.value='';buttons[1].listeners.click();assert(ids.content.textContent.includes('original_host_pending'));
 console.log('UI behavioral PASS');
});
"""
class TaskBoardUITests(unittest.TestCase):
    def test_node_render_filter_roadmap(self):
        if not shutil.which("node"): self.fail("Node required for JS behavioral test")
        run=subprocess.run(["node","-e",HARNESS,str(JS)],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn("UI behavioral PASS",run.stdout)
