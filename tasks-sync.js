/* Read-only metrics. No task contents or credentials are bundled. */
(function(root){
'use strict';
let data=null,error='',running=false,storageError=false;
const listeners=new Set(),cacheKey='task-metrics-cache-v1';
let key=new URLSearchParams(location.hash.slice(1)).get('key')||'';
try{if(key)localStorage.setItem('habits-access-v3',key);else key=localStorage.getItem('habits-access-v3')||'';data=JSON.parse(localStorage.getItem(cacheKey)||'null');}catch(_){storageError=true;}
function state(){const stale=!data?.data||Date.now()/1000-data.data.updated_at>90||data.sync.stale;return {data:data?.data||null,status:error||data?.sync.error?'error':stale||storageError?'pending':'ok',message:error||data?.sync.error||(storageError?'Данные загружены, но браузер не разрешил сохранить копию':stale?'Ожидаем свежие данные из Notion':'Синхронизировано с Notion')};}
function notify(){for(const fn of listeners)fn(state());}
async function refresh(){if(running)return;running=true;const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),12000);try{if(!key)throw new Error('Открой виджет из Notion: требуется ключ доступа.');const r=await fetch('https://153.76.192.118:8443/api/tasks/status',{cache:'no-store',headers:{Authorization:'Bearer '+key},signal:abort.signal});if(!r.ok)throw new Error(r.status===401?'Ключ доступа не принят. Открой виджет из Notion.':'Нет связи с сервером. Показаны последние сохранённые данные.');const next=await r.json();if(!next.data?.metrics)throw new Error('Ожидаем загрузку данных из Notion.');if(!data||next.generated_at>=data.generated_at){data=next;try{localStorage.setItem(cacheKey,JSON.stringify(data));}catch(_){storageError=true;}}error='';}catch(e){error=e.name==='AbortError'?'Нет ответа сервера. Показаны последние сохранённые данные.':e.message;}finally{clearTimeout(timeout);running=false;notify();}}
root.TaskSync={subscribe(fn){listeners.add(fn);fn(state());},refresh,state,start(){notify();refresh();setInterval(refresh,10000);setInterval(notify,10000);}};
root.addEventListener('online',refresh);root.addEventListener('focus',refresh);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})(window);
