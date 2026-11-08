/* Calendar metrics are deliberately separate from the durable delivery queue. */
(function(root){
'use strict';
const iso=d=>d.toISOString().slice(0,10),date=s=>new Date(s+'T00:00:00Z');
function fallbackPlan(data,key){
 const catalog=data.catalog||data.habits,history=data.stats.history;
 const daily=[],weekly={};
 for(const [id,h] of Object.entries(catalog)){
  const first=Object.keys(history).filter(d=>history[d].includes(id)).sort()[0]||h.created||data.today;
  const since=h.created&&h.created<first?h.created:first;
  if(key===data.today?!data.habits[id]:key<since)continue;
  if(h.type==='daily')daily.push(id);else weekly[id]=h.goal;
 }
 return {daily,weekly};
}
function plan(data,key){const fallback=fallbackPlan(data,key);return {daily:data.stats.daily_plans?.[key]||fallback.daily,weekly:data.stats.weekly_plans?.[key]||fallback.weekly};}
function day(data,key){
 const exists=Object.prototype.hasOwnProperty.call(data.stats.history,key),future=key>data.today,p=plan(data,key),done=data.stats.history[key]||[];
 const completed=p.daily.filter(id=>done.includes(id)),total=p.daily.length;
 const weeklyDone=Object.keys(p.weekly).filter(id=>done.includes(id));
 let status=future?'future':!exists?'unknown':!total?'no-plan':completed.length===total?'full':!completed.length?(key===data.today?'today':'zero'):'partial';
 return {key,ids:p.daily,done:completed,total,count:completed.length,weekly:p.weekly,weeklyDone,status,ratio:total?completed.length/total:0,known:exists&&!future};
}
function month(data,monthKey){
 const keys=Object.keys(data.stats.history).filter(d=>d.startsWith(monthKey)&&d<data.today).sort();
 let done=0,total=0,perfect=0,days=0;
 for(const key of keys){const d=day(data,key);if(!d.total)continue;done+=d.count;total+=d.total;days++;if(d.status==='full')perfect++;}
 if(data.today.startsWith(monthKey)&&day(data,data.today).status==='full')perfect++;
 return {done,total,perfect,days,percent:total?Math.round(done*100/total):null};
}
function week(data,key){
 const start=date(key);start.setUTCDate(start.getUTCDate()-(start.getUTCDay()+6)%7);const end=new Date(start);end.setUTCDate(end.getUTCDate()+6);
 const first=iso(start),last=iso(end),cutoff=last<data.today?last:data.today;
 const known=Object.keys(data.stats.history).filter(d=>d>=first&&d<=cutoff).sort();
 if(!known.length)return {first,last,items:[],done:0,total:0,known:false};
 const goals=plan(data,known[known.length-1]).weekly;
 const items=Object.entries(goals).map(([id,goal])=>{const dates=known.filter(d=>data.stats.history[d].includes(id));return {id,goal,count:dates.length,dates};});
 return {first,last,items,incomplete:known.length<Math.floor((date(cutoff)-start)/86400000)+1,done:items.reduce((n,h)=>n+Math.min(h.count,h.goal),0),total:items.reduce((n,h)=>n+h.goal,0),known:true};
}
root.HabitMetrics={day,month,week,plan,iso,date};
if(typeof module!=='undefined')module.exports=root.HabitMetrics;
})(typeof window==='undefined'?globalThis:window);
