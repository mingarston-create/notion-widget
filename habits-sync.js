/* Durable, shared outbox for both Notion embeds. No credentials in source. */
(function (root) {
  'use strict';
  const DB_NAME = 'notion-habits-durable-v3';
  let dbPromise, flushing = false, snapshot = null, outbox = [], error = '', storageError = '', remoteError = '', timer;
  const weekOnly = root.location.pathname?.endsWith('/habits.html');
  const cacheSlot = weekOnly ? 'snapshot-week' : 'snapshot-calendar';
  const statusPath = weekOnly ? '/status?scope=week' : '/status?scope=calendar';
  let cycle, checkedAt = 0, preparing = false, deliveryError = '';
  const subscribers = new Set();
  const pendingWrites = new Map();
  const params = new URLSearchParams(root.location.hash.slice(1));
  let key = params.get('key') || '';
  try { if (key) localStorage.setItem('habits-access-v3', key); else key = localStorage.getItem('habits-access-v3') || ''; } catch (_) {}
  // The fragment is deliberately not part of any HTTP request or Referer.
  function clock() { return snapshot?.clock || {timezone:'Europe/Moscow',cutoff_hour:3,revision:1}; }
  function effectiveDate(instant, timezone) {
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}).formatToParts(instant).map(p=>[p.type,p.value]));
    const d=new Date(parts.year+'-'+parts.month+'-'+parts.day+'T12:00:00Z');
    if(Number(parts.hour)<3)d.setUTCDate(d.getUTCDate()-1);
    return d.toISOString().slice(0,10);
  }
  function day() { return effectiveDate(new Date(),clock().timezone); }
  function newer(data,old) {
    if(old && data.server_started_at && old.server_started_at !== data.server_started_at) return data.server_started_at > (old.server_started_at || 0);
    return !old || ((data.clock?.revision||1)>=(old.clock?.revision||1) && data.revision>=old.revision && (data.generated_at||0)>=(old.generated_at||0)); }
  function openDB() {
    if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        db.createObjectStore('outbox', {keyPath: 'operation_id'}).createIndex('sequence', 'sequence', {unique: true});
        db.createObjectStore('meta');
      };
      request.onsuccess = () => {const db=request.result;db.onversionchange=()=>{db.close();dbPromise=null;};resolve(db);};
      request.onerror = () => reject(request.error);
      request.onblocked = () => {storageError='База занята другой вкладкой';notify();};
    }).catch(e=>{dbPromise=null;throw e;});
    return dbPromise;
  }
  async function transaction(stores, mode, fn) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      let tx;
      try { tx = db.transaction(stores, mode, {durability: 'strict'}); } catch (_) { tx = db.transaction(stores, mode); }
      let result;
      tx.oncomplete = () => resolve(typeof result === 'function' ? result() : result);
      tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error || new Error('Хранилище недоступно'));
      try { result = fn(tx); } catch (e) { tx.abort(); reject(e); }
    });
  }
  async function readLocal() {
    await transaction(['outbox', 'meta'], 'readonly', tx => {
      const q = tx.objectStore('outbox').index('sequence').getAll();
      const s = tx.objectStore('meta').get(cacheSlot);
      const previous = !snapshot ? tx.objectStore('meta').get('snapshot') : null;
      return () => { outbox = q.result || []; snapshot = s.result || previous?.result || snapshot; };
    }).then(fn => { if (typeof fn === 'function') fn(); });
  }
  function notify() { const value=state(); for (const callback of subscribers) callback(value); }
  function state() {
    const result = snapshot ? JSON.parse(JSON.stringify(snapshot)) : null;
    if (result) {
      result.today = day();
      result.stats.history[result.today] ||= [];
      // Accepted but not yet mirrored operations are also overlaid until their
      // receipt is visible in a fresh snapshot. The server snapshot is authoritative.
      const queuedIds = new Set(outbox.map(operation => operation.operation_id));
      const saving = [...pendingWrites.values()].filter(operation => !queuedIds.has(operation.operation_id));
      for (const op of [...outbox, ...saving]) {
        let list = result.stats.history[op.date] ||= [];
        list = list.filter(id => id !== op.id);
        if (op.done) list.push(op.id);
        result.stats.history[op.date] = list;
      }
      const week = new Date(result.today+'T00:00:00Z'); week.setUTCDate(week.getUTCDate() - (week.getUTCDay()+6)%7);
      const start = week.toISOString().slice(0,10);
      for (const [id,h] of Object.entries(result.habits)) {
        h.done_today = result.stats.history[result.today].includes(id);
        h.week_done = Object.entries(result.stats.history).filter(([d,arr]) => d >= start && d <= result.today && arr.includes(id)).length;
      }
      result.completed_today = result.stats.history[result.today];
      result.stats.today_count = result.completed_today.length;
      result.stats.total = Math.max(Object.keys(result.habits).length,result.stats.totals[result.today] || 0);
    }
    return {data:result,saving:pendingWrites.size,pending:outbox.length,error:storageError || error || (outbox.find(o=>o.blocked)?'Запись сохранена и требует проверки: '+outbox.find(o=>o.blocked).blocked:'') || deliveryError || remoteError,hasKey:!!key,ready:!!result && !storageError,blocked:outbox.some(o=>o.blocked)};
  }
  function renderStatus(element,value){
    const failure=value.error || (checkedAt && !preparing && value.data?.sync.error);
    const waiting=value.saving || value.pending || value.data?.sync.pending_days || value.data?.sync.projection_pending;
    const status=failure?'error':waiting?'pending':!checkedAt||preparing?'loading':'ok';
    const label=failure || (value.saving?'Сохраняем на устройстве':value.pending?'На устройстве ожидают отправки: '+value.pending:waiting?'Сохранено на сервере. Обновляем Notion':preparing?'Сервер готовит историю':!checkedAt?'Проверяем связь':'Синхронизировано с Notion');
    element.dataset.state=status;
    element.title=label;
    element.setAttribute('aria-label',label);
    element.setAttribute('role','button');
    element.tabIndex=0;
    element.onclick=()=>showStatus(element);
    element.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showStatus(element);}};
  }
  function showStatus(element){
    document.getElementById('habit-delivery-details')?.remove();
    const dialog=document.createElement('dialog');
    dialog.id='habit-delivery-details';
    dialog.style.cssText='max-width:min(85vw,26rem);padding:1rem;border:1px solid #aaa;border-radius:12px;font:14px system-ui';
    const line=document.createElement('p');
    const time=checkedAt || snapshot?.generated_at*1000;
    line.textContent=element.title+'. '+(checkedAt?'Ответ сервера: ':'Сохранённые данные: ')+(time?new Date(time).toLocaleString('ru-RU'):'ещё нет')+'. В очереди: '+outbox.length+'.';
    dialog.append(line);
    function button(text, action){
      const b=document.createElement('button');b.type='button';b.textContent=text;
      b.onclick=async()=>{b.disabled=true;try{await action();dialog.close();}catch{line.textContent='Не удалось применить выбор. Отметка осталась в очереди.';b.disabled=false;}};
      dialog.append(b);
    }
    const blocked=outbox.find(op=>op.blocked);
    if(blocked){
      const item=document.createElement('p');
      item.textContent=(snapshot?.catalog?.[blocked.id]?.title || 'Привычка')+' · '+blocked.date+' · на устройстве: '+(blocked.done?'отмечено':'снято')+'. '+blocked.blocked;
      dialog.append(item);
      button('Оставить на сервере',()=>resolveOperation(blocked,false));
      button('Применить мою отметку',()=>resolveOperation(blocked,true));
    }
    button('Закрыть',()=>{});
    dialog.onclose=()=>dialog.remove();
    document.body.append(dialog);dialog.showModal();
  }
  async function resolveOperation(blocked, useLocal){
    const data=await request(statusPath);
    if(!data.sync?.initialized)throw new Error('Сервер готовит данные');
    if(useLocal && data.journal_id && !window.confirm('Применить сохранённое на устройстве значение «'+(blocked.done?'отмечено':'снято')+'» за '+blocked.date+'?'))return;
    await transaction(['outbox'],'readwrite',tx=>{
      const store=tx.objectStore('outbox'),r=store.get(blocked.operation_id);
      r.onsuccess=()=>{
        if(!r.result?.blocked)return;
        if(!useLocal){store.delete(blocked.operation_id);return;}
        const next={...r.result};delete next.blocked;delete next.previous_operation;
        if(data.journal_id){next.base_revision=data.revision;next.journal_id=data.journal_id;next.base_operation=data.base_operation;}
        store.put(next);
      };
    });
    await saveSnapshot(data);await readLocal();deliveryError='';broadcast();notify();flush();
  }
  async function request(path, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(root.WidgetConfig.apiUrl('/api/habits'+path), {...options,cache:'no-store',signal:controller.signal,headers:{...root.WidgetConfig.authHeaders(key),'Content-Type':'application/json',...options.headers}});
      const value = await response.json().catch(() => ({}));
      if (!response.ok) { const e = new Error(value.error || 'Сервер временно недоступен'); e.status = response.status; throw e; }
      return value;
    } finally { clearTimeout(timeout); }
  }
  async function saveSnapshot(data) {
    await transaction(['meta'], 'readwrite', tx => {const meta=tx.objectStore('meta'),r=meta.get(cacheSlot);r.onsuccess=()=>{const old=r.result;if(newer(data,old)){meta.put(data,cacheSlot);snapshot=data;}else snapshot=old;};});
  }
  async function refresh() {
    if (!root.WidgetConfig.hasApi) {
      remoteError='Сервис данных ещё не подключён.';
      notify();
      return;
    }
    if (!key) { error='Открой виджет из Notion: требуется ключ доступа.';notify();return; }
    try {
      const localToken=snapshot?.token;
      const data=await request(statusPath+(localToken?'&since='+encodeURIComponent(localToken):''));
      if(localToken && data.unchanged===localToken){
        checkedAt=Date.now();preparing=false;remoteError='';notify();return;
      }
      checkedAt=Date.now();preparing=!data.sync.initialized;
      if(preparing){remoteError='';notify();return;}
      await saveSnapshot(data);
      remoteError='';error='';
      await readLocal();notify();
    } catch (e) { remoteError=e.status===401 ? 'Ключ доступа не принят. Открой виджет из Notion.' : 'Нет связи с сервером. Нажатия сохраняются на этом устройстве.';notify(); }
  }
  async function loadMonth(month){
    const slot='month:'+month;
    try{
      const data=await request('/status?scope=calendar&month='+encodeURIComponent(month));
      if(!data.sync?.initialized)throw new Error();
      await transaction(['meta'],'readwrite',tx=>tx.objectStore('meta').put(data,slot));
      return data;
    }catch{
      return transaction(['meta'],'readonly',tx=>{const r=tx.objectStore('meta').get(slot);return ()=>r.result;});
    }
  }
  async function setHabit(id, done, date) {
    if (!snapshot || storageError) throw new Error('Сначала нужно загрузить привычки и открыть локальное хранилище.');
    const instant=new Date(), captured=clock();
    const op = {operation_id:crypto.randomUUID(),id,date:date || effectiveDate(instant,captured.timezone),done,created_at:instant.toISOString()};
    if(snapshot.journal_id){
      op.base_revision=snapshot.revision;op.journal_id=snapshot.journal_id;op.base_operation=snapshot.base_operation;
      const previous=[...outbox,...pendingWrites.values()].reverse().find(item=>item.id===id && item.date===op.date);
      if(previous)op.previous_operation=previous.operation_id;
    }
    if(!date)Object.assign(op,{timezone:captured.timezone,clock_revision:captured.revision,cutoff_hour:3});
    pendingWrites.set(op.operation_id, op);
    error='';
    notify();
    try {
      await transaction(['meta','outbox'],'readwrite',tx => {
        const meta=tx.objectStore('meta'), request=meta.get('sequence');
        request.onsuccess=()=>{op.sequence=(request.result || 0)+1;meta.put(op.sequence,'sequence');tx.objectStore('outbox').add(op);};
      });
      await readLocal();
    } catch (e) {
      storageError='Не удалось сохранить отметку на устройстве.';
      throw e;
    } finally {
      pendingWrites.delete(op.operation_id);
      notify();
    }
    broadcast();
    flush();
  }
  async function drain(limit = Infinity) {
    let processed=0, deliveryFailed=false;
    const held=new Set(), accepted=[];
    await readLocal();
    for (const op of outbox) {
      const pair=op.date+'|'+op.id;
      if(op.blocked){held.add(pair);continue;}
      if(held.has(pair))continue;
      if(processed++>=limit)break;
      try {
        const ack=await request('/events',{method:'POST',body:JSON.stringify(op)});
        if (!ack.accepted || ack.operation_id!==op.operation_id) throw new Error('Нет подтверждения сервера');
        accepted.push(op.operation_id);
      } catch(e) {
        if(e.status===400 || e.status===409) {
          op.blocked=e.message;
          await transaction(['outbox'],'readwrite',tx=>tx.objectStore('outbox').put(op));
          held.add(pair);broadcast();continue;
        } else {deliveryFailed=true;deliveryError=e.status===401 ? 'Ключ доступа не принят. Очередь сохранена.' : 'Ожидаем соединения. Очередь сохранена на устройстве.';}
        break;
      }
    }
    if(accepted.length){
      // A lost receipt leaves the original IDs in IndexedDB for idempotent replay.
      let data;
      try{
        data=await request(statusPath);
        if(!data.sync?.initialized)throw new Error();
      }catch{deliveryError='Сервер принял отметки. Ожидаем подтверждения состояния.';await readLocal();notify();return;}
      await transaction(['outbox','meta'],'readwrite',tx=>{
        const meta=tx.objectStore('meta'),r=meta.get(cacheSlot);
        r.onsuccess=()=>{if(newer(data,r.result))meta.put(data,cacheSlot);};
        for(const id of accepted)tx.objectStore('outbox').delete(id);
      });
      checkedAt=Date.now();if(!deliveryFailed)deliveryError='';
      broadcast();
    }
    await readLocal();notify();
  }
  async function flush() {
    if(flushing || !key || storageError || !root.WidgetConfig.hasApi) return;
    flushing=true;
    try {
      // Serialize tabs; the persisted lease is the fallback for older browsers.
      if(navigator.locks) await navigator.locks.request('habits-outbox-v3',{ifAvailable:true},lock=>lock ? drain() : undefined);
      else {
        let acquired=false;const owner=crypto.randomUUID();
        await transaction(['meta'],'readwrite',tx=>{
          const meta=tx.objectStore('meta'),r=meta.get('lease');
          r.onsuccess=()=>{if(!r.result || r.result.until<Date.now()){acquired=true;meta.put({owner,until:Date.now()+60000},'lease');}};
        });
        if(acquired) {
          try {await drain(1);} finally {await transaction(['meta'],'readwrite',tx=>{const m=tx.objectStore('meta'),r=m.get('lease');r.onsuccess=()=>{if(r.result?.owner===owner)m.delete('lease');};});}
        }
      }
    } catch(e) { storageError='Локальное хранилище недоступно. Отметки не принимаются.';notify(); }
    finally {flushing=false;}
  }
  const channel = typeof BroadcastChannel!=='undefined' ? new BroadcastChannel('habits-v3') : null;
  function broadcast(){channel?.postMessage(cacheSlot);}
  if(channel)channel.onmessage=async e=>{try{await readLocal();notify();if(e.data!==cacheSlot)tick();}catch{storageError='Хранилище временно недоступно';notify();}};
  async function runCycle(){
    try{await readLocal();storageError='';}catch{storageError='Локальное хранилище недоступно. Повторим проверку.';notify();return;}
    notify();
    await Promise.all([refresh(),flush()]);
  }
  function tick(){
    if(document.hidden)return;
    if(!cycle)cycle=runCycle().finally(()=>{cycle=null;});
    return cycle;
  }
  function start(){
    if(!timer)timer=setInterval(tick,10000);
    if(navigator.storage?.persist)navigator.storage.persist().catch(()=>{});
    return tick();
  }
  root.addEventListener('online',tick);
  root.addEventListener('focus',tick);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
  root.HabitsSync={start,subscribe(fn){subscribers.add(fn);fn(state());return ()=>subscribers.delete(fn);},setHabit,loadMonth,refresh:tick,state,day,effectiveDate,renderStatus};
})(window);
