/* Durable, shared outbox for both Notion embeds. No credentials in source. */
(function (root) {
  'use strict';
  const API = 'https://153.76.192.118:8443/api/habits';
  const DB_NAME = 'notion-habits-durable-v3';
  let dbPromise, flushing = false, snapshot = null, outbox = [], error = '', storageError = '', remoteError = '', timer;
  const subscribers = new Set();
  const params = new URLSearchParams(root.location.hash.slice(1));
  let key = params.get('key') || '';
  try { if (key) localStorage.setItem('habits-access-v3', key); else key = localStorage.getItem('habits-access-v3') || ''; } catch (_) {}
  // The fragment is deliberately not part of any HTTP request or Referer.
  function day() { return new Date().toISOString().slice(0, 10); }
  function openDB() {
    if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        db.createObjectStore('outbox', {keyPath: 'operation_id'}).createIndex('sequence', 'sequence', {unique: true});
        db.createObjectStore('meta');
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Хранилище заблокировано другой вкладкой'));
    });
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
      const s = tx.objectStore('meta').get('snapshot');
      return () => { outbox = q.result || []; snapshot = s.result || snapshot; };
    }).then(fn => { if (typeof fn === 'function') fn(); });
  }
  function notify() { for (const callback of subscribers) callback(state()); }
  function state() {
    const result = snapshot ? JSON.parse(JSON.stringify(snapshot)) : null;
    if (result) {
      result.today = day();
      result.stats.history[result.today] ||= [];
      // Accepted but not yet mirrored operations are also overlaid until their
      // receipt is visible in a fresh snapshot. The server snapshot is authoritative.
      for (const op of outbox) {
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
    return {data:result,pending:outbox.length,error:storageError || error || (outbox.find(o=>o.blocked)?'Запись сохранена и требует проверки: '+outbox.find(o=>o.blocked).blocked:'') || remoteError,hasKey:!!key,ready:!!result && !storageError,blocked:outbox.some(o=>o.blocked)};
  }
  async function request(path, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(API+path, {...options,cache:'no-store',signal:controller.signal,headers:{'Authorization':'Bearer '+key,'Content-Type':'application/json',...options.headers}});
      const value = await response.json().catch(() => ({}));
      if (!response.ok) { const e = new Error(value.error || 'Сервер временно недоступен'); e.status = response.status; throw e; }
      return value;
    } finally { clearTimeout(timeout); }
  }
  async function saveSnapshot(data) {
    await transaction(['meta'], 'readwrite', tx => {const meta=tx.objectStore('meta'),r=meta.get('snapshot');r.onsuccess=()=>{const old=r.result;if(!old || (data.revision>=old.revision && data.today>=old.today)){meta.put(data,'snapshot');snapshot=data;}else snapshot=old;};});
  }
  async function refresh() {
    if (!key) { error='Открой виджет из Notion: требуется ключ доступа.';notify();return; }
    try {
      const data=await request('/status');
      if (!data.sync.initialized) throw new Error('Подготавливаем историю. Попробуем снова автоматически.');
      await saveSnapshot(data); remoteError='';error='';await readLocal();notify();
    } catch (e) { remoteError=e.status===401 ? 'Ключ доступа не принят. Открой виджет из Notion.' : 'Нет связи с сервером. Нажатия сохраняются на этом устройстве.';notify(); }
  }
  async function setHabit(id, done, date = day()) {
    if (!snapshot || storageError) throw new Error('Сначала нужно загрузить привычки и открыть локальное хранилище.');
    const op = {operation_id:crypto.randomUUID(),id,date,done,created_at:new Date().toISOString()};
    try {
      await transaction(['meta','outbox'],'readwrite',tx => {
        const meta=tx.objectStore('meta'), request=meta.get('sequence');
        request.onsuccess=()=>{op.sequence=(request.result || 0)+1;meta.put(op.sequence,'sequence');tx.objectStore('outbox').add(op);};
      });
      await readLocal();error='';notify();broadcast();flush();
    } catch (e) { storageError='Не удалось сохранить на устройстве. Отметка не принята.';notify();throw e; }
  }
  async function drain(limit = Infinity) {
    let processed=0;
    await readLocal();
    for (const op of outbox) {
      if(processed++>=limit)break;
      if (op.blocked) break;
      try {
        const ack=await request('/events',{method:'POST',body:JSON.stringify(op)});
        if (!ack.accepted || ack.operation_id!==op.operation_id) throw new Error('Нет подтверждения сервера');
        // Refresh BEFORE deleting the outbox item: otherwise a stale cached
        // snapshot can briefly erase a just accepted click after reload.
        const data=await request('/status');
        await transaction(['outbox','meta'],'readwrite',tx=>{const meta=tx.objectStore('meta'),r=meta.get('snapshot');r.onsuccess=()=>{const old=r.result;if(!old || (data.revision>=old.revision && data.today>=old.today)){meta.put(data,'snapshot');snapshot=data;}else snapshot=old;};tx.objectStore('outbox').delete(op.operation_id);});error='';remoteError='';
      } catch(e) {
        if(e.status===400) {
          op.blocked=e.message;
          await transaction(['outbox'],'readwrite',tx=>tx.objectStore('outbox').put(op));
          error='Запись сохранена, но требует проверки: '+e.message;
        } else remoteError=e.status===401 ? 'Ключ доступа не принят. Очередь сохранена.' : 'Ожидаем соединения. Очередь сохранена на устройстве.';
        break;
      }
      await readLocal();notify();broadcast();
    }
    await readLocal();notify();
  }
  async function flush() {
    if(flushing || !key || storageError) return;
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
    finally {flushing=false;if(outbox.length&&!outbox[0].blocked&&!remoteError&&!storageError)setTimeout(flush,0);}
  }
  const channel = typeof BroadcastChannel!=='undefined' ? new BroadcastChannel('habits-v3') : null;
  function broadcast(){channel?.postMessage('changed');}
  if(channel)channel.onmessage=async()=>{await readLocal();notify();};
  async function tick(){await flush();await refresh();}
  async function start() {
    try {await readLocal();notify();} catch(e) {storageError='Браузер запретил локальное хранение. Отметки не принимаются.';notify();return;}
    if (navigator.storage?.persist) navigator.storage.persist().catch(()=>{});
    await tick();timer=setInterval(tick,10000);
  }
  root.addEventListener('online',tick);
  root.addEventListener('focus',tick);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
  root.HabitsSync={start,subscribe(fn){subscribers.add(fn);fn(state());return ()=>subscribers.delete(fn);},setHabit,refresh:tick,state,day};
})(window);
