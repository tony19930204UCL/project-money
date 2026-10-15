(function(){
"use strict";
let last=null,view="tasks";
const $=id=>document.getElementById(id);
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text??"—");if(cls)n.className=cls;return n}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild)}
function add(parent,tag,text,cls){const n=el(tag,text,cls);parent.appendChild(n);return n}
function status(t){if(t.blocker||t.lifecycle==="BLOCKED")return "BLOCKED";if(t.running)return "RUNNING";if(t.scheduled)return "SCHEDULED";return t.lifecycle}
function render(){
 if(!last)return;
 const tasks=last.tasks||[],sum=$("summary");clear(sum);
 const counts=[["執行中",t=>t.running],["已排程",t=>t.scheduled],["未開始",t=>t.lifecycle==="PLANNED"||t.lifecycle==="NOT_STARTED"],["阻塞",t=>status(t)==="BLOCKED"],["待驗收",t=>t.awaiting_acceptance],["逾期",t=>t.overdue]];
 counts.forEach(([label,fn])=>{const c=add(sum,"div",undefined,"metric");add(c,"strong",tasks.filter(fn).length);add(c,"span",label)});
 const errors=$("errors");clear(errors);
 (last.diagnostics||[]).filter(d=>d.status==="ERROR"||d.status==="NOT_CONFIGURED").forEach(d=>add(errors,"p",d.source+"： "+(d.status==="ERROR"?"來源錯誤":"未設定")+" "+(d.message||""),"warning"));
 const selector=$("project"),old=selector.value;clear(selector);selector.appendChild(new Option("全部",""));
 [...new Set(tasks.map(t=>t.project).filter(Boolean))].sort().forEach(p=>selector.appendChild(new Option(p,p)));selector.value=old;
 const content=$("content");clear(content);
 if(view==="roadmap"){(last.projects||[]).forEach(p=>{const box=add(content,"article",undefined,"task");add(box,"h2",p.title||p.id);(p.stages||[]).forEach(s=>{const stage=add(box,"div",undefined,"stage");add(stage,"h3",s.title||s.id);add(stage,"p","觀察狀態："+(s.observed_state||"待確認"));add(stage,"p","驗收範圍："+(s.acceptance_scope||"未驗收"));add(stage,"p","關聯任務："+(s.task_ids||[]).join(", "))})});return}
 let shown=tasks.filter(t=>(!$("search").value||((t.id+" "+t.title).toLowerCase().includes($("search").value.toLowerCase())))&&(!$("status").value||status(t)===$("status").value)&&(!$("project").value||t.project===$("project").value));
 if(view==="changes")shown=shown.filter(t=>t.last_activity_at).sort((a,b)=>String(b.last_activity_at).localeCompare(String(a.last_activity_at)));
 else shown.sort((a,b)=>Number(!!b.blocker)-Number(!!a.blocker)||Number(!!b.overdue)-Number(!!a.overdue)||String(a.due_at||"~").localeCompare(String(b.due_at||"~")));
 shown.forEach(t=>{const box=add(content,"article",undefined,"task");add(box,"h3",t.title);add(box,"p",t.id+" · "+status(t)+" · "+(t.project||"未歸類"));add(box,"p","執行器："+t.executor_kind+" / "+t.executor_health,"detail");add(box,"p","最新結果："+(t.latest_result||"無證據"),"detail");add(box,"p","驗收範圍："+(t.acceptance_scope||"未確認"),"detail");add(box,"p","期限："+(t.due_at||"未提供")+(t.overdue?"（逾期）":""),"detail");add(box,"p","依賴："+(t.dependencies||[]).join(", "),"detail");if(t.blocker)add(box,"p","阻塞："+t.blocker,"warning")});
}
async function refresh(){
 try{const response=await fetch("/api/status",{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);const data=await response.json();last=data;$("updated").textContent="最近成功讀取："+data.polled_at;render()}
 catch(e){$("errors").textContent="讀取失敗，保留上次有效資料："+e.message;if(!last)$("updated").textContent="尚無有效資料"}
}
["search","status","project"].forEach(id=>$(id).addEventListener("input",render));
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>{view=b.getAttribute("data-view");render()}));
refresh();setInterval(refresh,10000);
})();