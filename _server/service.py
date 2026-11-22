"""Durable habit journal. Notion is the catalog and the published daily archive."""
import datetime as dt
import hashlib
import hmac
import http.server
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from zoneinfo import ZoneInfo

DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
ORIGIN = 'https://mingarston-create.github.io'

def utc_now():
    return dt.datetime.now(dt.timezone.utc)

def effective_date(instant, timezone):
    local=instant.astimezone(ZoneInfo(timezone))
    return (local.date()-dt.timedelta(days=1 if local.hour<3 else 0)).isoformat()

def today(timezone='Europe/Moscow'):
    return effective_date(utc_now(),timezone)


def streaks(history, day):
    best = run = 0
    previous = None
    for key in sorted(k for k in history if k <= day):
        date = dt.date.fromisoformat(key)
        run = (run + 1 if previous == date - dt.timedelta(days=1) else 1) if history[key] else 0
        best = max(best, run)
        previous = date
    date = dt.date.fromisoformat(day)
    if not history.get(day): date -= dt.timedelta(days=1)
    current = 0
    while history.get(date.isoformat()):
        current += 1
        date -= dt.timedelta(days=1)
    return current, best

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:return super().__exit__(*args)
        finally:self.close()

class Store:
    def __init__(self, path, legacy_aliases=None):
        self.path = str(path)
        self.legacy_aliases = legacy_aliases or {}
        self._clock_export_lock = threading.Lock()
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS habits(id TEXT PRIMARY KEY, data TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, done TEXT NOT NULL, total INTEGER NOT NULL, page TEXT, version INTEGER NOT NULL DEFAULT 1, synced INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, date TEXT NOT NULL, habit TEXT NOT NULL, done INTEGER NOT NULL, accepted REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            if 'plan' not in [r[1] for r in c.execute('PRAGMA table_info(days)')]:c.execute('ALTER TABLE days ADD COLUMN plan TEXT')
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        return c
    def meta(self, key, default=None):
        with self.connect() as c:
            r = c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return json.loads(r[0]) if r else default
    def set_meta(self, key, value):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))
    def clock(self):
        # Publish only the non-secret timezone/revision, atomically. Consumers
        # do not need database access or permission to create SQLite WAL files.
        with self._clock_export_lock:
            value=self.meta('clock',{'timezone':'Europe/Moscow','cutoff_hour':3,'revision':1,'primary_device':None})
            target=Path(self.path).parent/'bot-clock'/'clock.json'
            content=json.dumps({'timezone':value['timezone'],'revision':value['revision']})
            try:
                if not target.exists() or target.read_text()!=content:
                    target.parent.mkdir(mode=0o750,exist_ok=True)
                    temporary=target.with_suffix('.tmp')
                    temporary.write_text(content)
                    temporary.chmod(0o640)
                    temporary.replace(target)
            except OSError:
                logging.warning('Could not publish bot timezone')
            return value
    def day(self):
        return today(self.clock()['timezone'])
    def configure_clock(self, configured_zone):
        """Freeze the existing workspace zone unless an administrator selects one."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT value FROM meta WHERE key='clock'").fetchone()
            old = json.loads(row[0]) if row else {
                'timezone': 'Europe/Moscow', 'cutoff_hour': 3, 'revision': 1,
            }
            zone = old['timezone'] if configured_zone is None else configured_zone
            ZoneInfo(zone)  # Fail startup on an invalid setting before changing the journal.
            changed = zone != old['timezone']
            if row and not changed and 'primary_device' not in old:
                return
            revision = old['revision'] + int(changed)
            settings = {'timezone': zone, 'cutoff_hour': 3, 'revision': revision}
            row = c.execute("SELECT value FROM meta WHERE key='clock_history'").fetchone()
            history = json.loads(row[0]) if row else {}
            history.setdefault(str(old['revision']), {'timezone': old['timezone'], 'cutoff_hour': 3})
            history[str(revision)] = {'timezone': zone, 'cutoff_hour': 3}
            c.executemany('INSERT OR REPLACE INTO meta VALUES (?, ?)', [
                ('clock', json.dumps(settings)), ('clock_history', json.dumps(history)),
            ])
            if changed:
                c.execute('UPDATE days SET version=version+1 WHERE date IN (?, ?)',
                          (today(old['timezone']), today(zone)))

    def catalog(self, active=False):
        with self.connect() as c:
            return {r['id']:json.loads(r['data']) for r in c.execute('SELECT * FROM habits'+(' WHERE active=1' if active else ''))}
    def refresh_catalog(self, rows):
        with self.connect() as c:
            c.execute('UPDATE habits SET active=0')
            for row in rows:
                p=row['properties']; title=''.join(x.get('plain_text',x.get('text',{}).get('content','')) for x in p.get('Привычка',{}).get('title',[])).strip()
                if not title: continue
                goal=p.get('Цель в неделю',{}).get('number')
                if goal is None: goal=7
                goal=max(1,min(7,int(goal)))
                old=c.execute('SELECT data FROM habits WHERE id=?',(row['id'],)).fetchone()
                aliases=json.loads(old[0]).get('aliases',[]) if old else []
                icon=(row.get('icon') or {}).get('emoji','🌱')
                label=icon+' '+title
                aliases=list(dict.fromkeys(aliases+[label]))
                data={'id':row['id'],'title':title,'icon':icon,'goal':goal,'type':'daily' if goal==7 else 'weekly','aliases':aliases,'created':row.get('created_time','')[:10]}
                c.execute('INSERT OR REPLACE INTO habits VALUES (?,?,1)',(row['id'],json.dumps(data,ensure_ascii=False)))
        self.set_meta('catalog_at',time.time())
    def import_history(self, logs, habits):
        if self.meta('initialized'): return
        catalog=self.catalog()
        names={}
        for key,h in catalog.items():
            for alias in h['aliases']+[h['title']]:names[alias]=key
        names.update(self.legacy_aliases)
        with self.connect() as c:
            for row in logs:
                p=row['properties']; date=(p.get('Дата',{}).get('date') or {}).get('start','')[:10]
                if not date: continue
                done={}
                for tag in p.get('Привычки',{}).get('multi_select',[]):
                    name=tag['name']; key=names.get(name)
                    if not key:
                        key=next((k for k,h in catalog.items() if name.endswith(' '+h['title'])),None)
                    if not key:key='legacy-'+hashlib.sha256(name.encode()).hexdigest()[:20]
                    done[key]=name
                # Never silently discard duplicate historical rows or discrepant counts.
                if c.execute('SELECT 1 FROM days WHERE date=?',(date,)).fetchone():raise ValueError('Duplicate historical day: '+date)
                if (p.get('Выполнено',{}).get('number') or 0)!=len(done):raise ValueError('Historical count mismatch: '+date)
                total=p.get('Всего',{}).get('number') or len(catalog)
                c.execute('INSERT INTO days(date,done,total,page) VALUES (?,?,?,?)',(date,json.dumps(done,ensure_ascii=False),int(total),row['id']))
            # Preserve any current checkbox not yet reflected in the journal.
            day=self.day(); col=DAYS[dt.date.fromisoformat(day).weekday()]
            row=c.execute('SELECT * FROM days WHERE date=?',(day,)).fetchone()
            done=json.loads(row['done']) if row else {}
            for h in habits:
                if h['properties'].get(col,{}).get('checkbox') and h['id'] in catalog:
                    item=catalog[h['id']];done[h['id']]=item['icon']+' '+item['title']
            if row:c.execute('UPDATE days SET done=?,version=version+1 WHERE date=?',(json.dumps(done,ensure_ascii=False),day))
            else:c.execute('INSERT INTO days(date,done,total) VALUES (?,?,?)',(day,json.dumps(done,ensure_ascii=False),len(catalog)))
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('initialized','true'))
    def ensure_days(self, day=None):
        day=day or self.day()
        if not self.meta('initialized'):return
        with self.connect() as c:
            last=c.execute('SELECT MAX(date) FROM days').fetchone()[0] or day
            count=c.execute('SELECT COUNT(*) FROM habits WHERE active=1').fetchone()[0]
            d=dt.date.fromisoformat(min(last,day))
            end=dt.date.fromisoformat(day)
            while d<=end:
                c.execute('INSERT OR IGNORE INTO days(date,done,total) VALUES (?, ?, ?)',(d.isoformat(),'{}',count));d+=dt.timedelta(days=1)
            c.execute('UPDATE days SET total=?,version=version+1 WHERE date=? AND total<?',(count,day,count))
            old=c.execute('SELECT value FROM meta WHERE key="last_day"').fetchone()
            if not old or json.loads(old[0])!=day:
                c.execute('UPDATE days SET version=version+1 WHERE date=?',((end-dt.timedelta(days=1)).isoformat(),))
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('last_day',json.dumps(day)))
    def ensure_plans(self, day=None):
        # Freeze each day's eligible habits: later additions/goal changes must not
        # recolor past days. For pre-migration history, use earliest observed activity.
        day=day or self.day()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            catalog={r['id']:(json.loads(r['data']),r['active']) for r in c.execute('SELECT * FROM habits')}
            rows=list(c.execute('SELECT * FROM days ORDER BY date'))
            first={key:h.get('created') or day for key,(h,active) in catalog.items()}
            for row in rows:
                for key in json.loads(row['done']):
                    if key in first:first[key]=min(first[key],row['date'])
            newest=max([r['date'] for r in rows]+[day])
            for row in rows:
                if row['plan'] is not None and (row['date']!=day or day<newest):continue
                eligible={key:h for key,(h,active) in catalog.items() if (active if row['date']==day else first[key]<=row['date'])}
                plan={'daily':[key for key,h in eligible.items() if h['type']=='daily'],'weekly':{key:h['goal'] for key,h in eligible.items() if h['type']=='weekly'}}
                encoded=json.dumps(plan,sort_keys=True)
                if row['plan']!=encoded:c.execute('UPDATE days SET plan=?,version=version+1 WHERE date=?',(encoded,row['date']))

    def accept(self, data):
        if not self.meta('initialized'):raise RuntimeError('Initialization pending')
        op=data.get('operation_id');key=data.get('id');date=data.get('date');done=data.get('done')
        if not isinstance(op,str) or not 16<=len(op)<=100 or not isinstance(key,str) or type(done) is not bool:raise ValueError('Invalid operation')
        try:d=dt.date.fromisoformat(date)
        except (TypeError,ValueError):raise ValueError('Invalid date')
        if date!=d.isoformat() or date<'2026-11-01':raise ValueError('Date outside supported range')
        context=None
        if 'timezone' in data or 'clock_revision' in data:
            zone=data.get('timezone');revision=data.get('clock_revision')
            known=self.meta('clock_history',{'1':{'timezone':'Europe/Moscow','cutoff_hour':3}}).get(str(revision))
            if not known or known['timezone']!=zone or data.get('cutoff_hour')!=3:raise ValueError('Unknown captured timezone settings')
            try:instant=dt.datetime.fromisoformat(data['created_at'].replace('Z','+00:00'))
            except (KeyError,TypeError,ValueError):raise ValueError('Invalid captured timestamp')
            if instant.tzinfo is None or instant>utc_now()+dt.timedelta(minutes=5):raise ValueError('Invalid device clock')
            if effective_date(instant,zone)!=date:raise ValueError('Captured date does not match captured timezone')
            context={k:data[k] for k in ('timezone','clock_revision','cutoff_hour','created_at')}
        elif date>max(today(),self.day()):
            # Old browser queues already contain a fixed UTC-based date. Keep it.
            raise ValueError('Date outside supported range')
        fingerprint=json.dumps([key,date,done]+([context] if context else []),separators=(',',':'))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT fingerprint FROM operations WHERE id=?',(op,)).fetchone()
            if existing:
                if existing[0]!=fingerprint:raise ValueError('Operation ID reused with different content')
                return {'ok':True,'operation_id':op,'accepted':True,'duplicate':True}
            h=c.execute('SELECT data FROM habits WHERE id=?',(key,)).fetchone()
            if not h:raise ValueError('Unknown habit; waiting for catalog refresh may help')
            h=json.loads(h[0]); row=c.execute('SELECT * FROM days WHERE date=?',(date,)).fetchone()
            values=json.loads(row['done']) if row else {}
            if done:values[key]=h['icon']+' '+h['title']
            else:values.pop(key,None)
            encoded=json.dumps(values,ensure_ascii=False)
            if row:c.execute('UPDATE days SET done=?,version=version+1 WHERE date=?',(encoded,date))
            else:
                total=c.execute('SELECT COUNT(*) FROM habits WHERE active=1').fetchone()[0]
                c.execute('INSERT INTO days(date,done,total) VALUES (?,?,?)',(date,encoded,total))
            # A new habit increases today's denominator without rewriting historical totals.
            if date==self.day():c.execute('UPDATE days SET total=MAX(total,(SELECT COUNT(*) FROM habits WHERE active=1),?) WHERE date=?',(len(values),date))
            c.execute('UPDATE days SET version=version+1 WHERE date>?',(date,))
            c.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',(op,fingerprint,date,key,int(done),time.time()))
        return {'ok':True,'operation_id':op,'accepted':True}
    def snapshot(self):
        clock=self.clock();day=today(clock['timezone'])
        self.ensure_days(day)
        self.ensure_plans(day)
        catalog=self.catalog(); active=self.catalog(True)
        with self.connect() as c:
            c.execute('BEGIN')
            rows=list(c.execute('SELECT * FROM days ORDER BY date'))
            revision=c.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
        history={r['date']:list(json.loads(r['done'])) for r in rows}
        totals={r['date']:r['total'] for r in rows}
        start=dt.date.fromisoformat(day);start-=dt.timedelta(days=start.weekday())
        for key,h in active.items():
            h['done_today']=key in history.get(day,[])
            h['week_done']=sum(key in values for date,values in history.items() if start.isoformat()<=date<=day)
        plans={r['date']:json.loads(r['plan'] or '{}') for r in rows}
        daily_history={date:[key for key in ids if key in plans.get(date,{}).get('daily',[])] for date,ids in history.items()}
        cur,best=streaks(daily_history,day)
        return {'revision':revision,'generated_at':time.time(),'clock':clock,'today':day,'timezone':clock['timezone'],'cutoff':'03:00','habits':active,'catalog':catalog,'completed_today':history.get(day,[]),'stats':{'daily_plans':{date:p.get('daily',[]) for date,p in plans.items()},'weekly_plans':{date:p.get('weekly',{}) for date,p in plans.items()},'history':history,'totals':totals,'today_count':len(history.get(day,[])),'total':max(totals.get(day,len(active)),len(active)),'current_streak':cur,'best_streak':best},'sync':{'pending_days':sum(r['version']!=r['synced'] for r in rows),'last_notion_sync':self.meta('last_sync'),'catalog_at':self.meta('catalog_at'),'error':self.meta('sync_error'),'initialized':self.meta('initialized',False)}}

class Notion:
    def __init__(self, token, databases):
        self.token = token
        self.catalog_id = databases['habits_catalog']
        self.log_id = databases['habits_log']
        self.tasks_id = databases['tasks']
    def request(self, path, body=None, method=None):
        req=urllib.request.Request('https://api.notion.com/v1/'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Authorization':'Bearer '+self.token,'Notion-Version':'2022-06-28','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
    def query(self, db, body=None):
        body=dict(body or {});body['page_size']=100;rows=[]
        while True:
            data=self.request('databases/'+db+'/query',body)
            rows.extend(data['results'])
            if not data.get('has_more'):return rows
            body['start_cursor']=data['next_cursor']

def rating(n, total, date, current_day=None):
    if not n:return '⏳ В процессе' if date>=(current_day or today()) else '💤 Пропуск'
    ratio=n/max(total,1)
    if ratio>=1:return '🔥 Идеально'
    if ratio>=.75:return '⚡ Отлично'
    if ratio>=.5:return '👍 Хорошо'
    return '🌱 Начало'

def sync_day(store, notion, row):
    day=row['date'];values=json.loads(row['done'])
    page=row['page']
    if not page:
        found=notion.query(notion.log_id,{'filter':{'property':'Дата','date':{'equals':day}}})
        if len(found)>1:raise RuntimeError('Duplicate daily pages require review')
        if found:page=found[0]['id']
        elif time.time()-store.meta('uncertain_create:'+day,0)<120:raise RuntimeError('Waiting to reconcile an uncertain Notion page creation')
    snapshot=store.snapshot();history=snapshot['stats']['history']
    daily_ids=snapshot['stats']['daily_plans'].get(day,[])
    daily_done=sum(key in daily_ids for key in values)
    weekly_ids=snapshot['stats']['weekly_plans'].get(day,{})
    daily_history={date:[key for key in ids if key in snapshot['stats']['daily_plans'].get(date,[])] for date,ids in history.items()}
    cur,_=streaks(daily_history,day)
    if day<snapshot['today'] and not daily_done:cur=0
    date=dt.date.fromisoformat(day)
    title='Сегодня — '+date.strftime('%d.%m.%Y') if day==snapshot['today'] else date.strftime('%d.%m.%Y')+' · '+DAYS[date.weekday()]
    props={'День':{'title':[{'text':{'content':title}}]},'Дата':{'date':{'start':day}},'Выполнено':{'number':len(values)},'Всего':{'number':row['total']},'Привычки':{'multi_select':[{'name':v} for v in values.values()]},'Стрик':{'number':cur},'Оценка дня':{'select':{'name':rating(daily_done,len(daily_ids),day,snapshot['today'])}}}
    if store.meta('daily_schema'):
        props.update({'Ежедневных выполнено':{'number':daily_done},'Ежедневных всего':{'number':len(daily_ids)},'Недельных выполнено':{'number':sum(key in weekly_ids for key in values)}})
    if page:
        existing=notion.request('pages/'+page)
        old_title=''.join(t.get('plain_text',t.get('text',{}).get('content','')) for t in existing.get('properties',{}).get('День',{}).get('title',[]))
        if day!=snapshot['today'] and not old_title.startswith('Сегодня'):props.pop('День')
        notion.request('pages/'+page,{'properties':props},'PATCH')
    else:
        try:page=notion.request('pages',{'parent':{'database_id':notion.log_id},'properties':props})['id']
        except Exception:
            store.set_meta('uncertain_create:'+day,time.time())
            raise
    with store.connect() as c:c.execute('UPDATE days SET page=?,synced=? WHERE date=?',(page,row['version'],day))
    store.set_meta('last_sync',time.time())

def sync_projection(store, notion, rows):
    snap=store.snapshot(); history=snap['stats']['history'];start=dt.date.fromisoformat(snap['today']);start-=dt.timedelta(days=start.weekday())
    for row in rows:
        props={}
        for index,col in enumerate(DAYS):
            value=row['id'] in history.get((start+dt.timedelta(days=index)).isoformat(),[])
            if row['properties'].get(col,{}).get('checkbox',False)!=value:props[col]={'checkbox':value}
        if store.meta('clock_projection_schema'):
            value=row['id'] in history.get(snap['today'],[])
            if row['properties'].get('Сегодня отмечено',{}).get('checkbox',False)!=value:props['Сегодня отмечено']={'checkbox':value}
        if props:notion.request('pages/'+row['id'],{'properties':props},'PATCH');time.sleep(.36)

class Worker(threading.Thread):
    def __init__(self,store,notion):
        super().__init__(daemon=True);self.store=store;self.notion=notion;self.wake=threading.Event();self.stop=threading.Event()
    def run(self):
        last_catalog=0;last_projection=0;backoff=1;rows=[];last_clock_revision=None
        while not self.stop.is_set():
            try:
                if time.time()-last_catalog>=30 or not self.store.meta('initialized'):
                    rows=self.notion.query(self.notion.catalog_id);self.store.refresh_catalog(rows);last_catalog=time.time()
                if not self.store.meta('initialized'):self.store.import_history(self.notion.query(self.notion.log_id),rows)
                self.store.ensure_days()
                self.store.ensure_plans()
                with self.store.connect() as c:dirty=list(c.execute('SELECT * FROM days WHERE version!=synced ORDER BY date'))
                for row in dirty:
                    sync_day(self.store,self.notion,row);time.sleep(.4)
                if dirty or time.time()-last_projection>=60 or last_clock_revision!=self.store.clock()['revision']:
                    # Fetch fresh checkboxes; never treat them as the primary history after import.
                    fresh=self.notion.query(self.notion.catalog_id);sync_projection(self.store,self.notion,fresh);last_projection=time.time();last_clock_revision=self.store.clock()['revision']
                self.store.set_meta('sync_error',None);backoff=1
            except Exception as error:
                # Do not log credentials, URLs with secrets, or HTTP response bodies.
                logging.warning('Notion synchronization deferred: %s',type(error).__name__)
                self.store.set_meta('sync_error','Notion временно недоступен. Записи сохранены на сервере.')
                backoff=min(60, max(5,backoff*2))
            self.wake.wait(backoff);self.wake.clear()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,code,data):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('Access-Control-Allow-Origin',ORIGIN)
        self.send_header('Vary','Origin');self.end_headers();self.wfile.write(raw)
    def authorized(self, expected):
        scheme, _, given = self.headers.get('Authorization', '').partition(' ')
        if not expected or scheme.lower() != 'bearer' or not hmac.compare_digest(given.encode(), expected.encode()):
            self.reply(401, {'error':'Требуется ключ доступа из виджета Notion.'})
            return False
        return True

    def donation_status(self):
        if not self.authorized(self.server.donation_api_key):
            return
        try:
            snapshot = json.loads(self.server.donation_data.read_text())
        except (OSError, ValueError):
            self.reply(503, {'error':'Данные донорства пока недоступны.'})
            return
        self.reply(200, snapshot)
    def do_OPTIONS(self):
        self.send_response(204);self.send_header('Access-Control-Allow-Origin',ORIGIN);self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS');self.send_header('Access-Control-Allow-Headers','Content-Type, Authorization');self.end_headers()
    def do_GET(self):
        if self.path=='/health':self.reply(200,{'status':'ok'});return
        if self.path.split('?')[0]=='/api/donation/status':
            self.donation_status()
            return
        if not self.authorized(self.server.api_key):return
        if self.path.split('?')[0]=='/api/tasks/status':self.reply(200,self.server.tasks.snapshot());return
        if self.path.split('?')[0]!='/api/habits/status':self.reply(404,{'error':'Not found'});return
        self.reply(200,self.server.store.snapshot())
    def do_POST(self):
        if not self.authorized(self.server.api_key):return
        if self.path.split('?')[0] not in ['/api/habits/events','/api/habits/toggle']:self.reply(404,{'error':'Not found'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            result=self.server.store.accept(data)
            self.server.worker.wake.set();self.reply(202,result)
        except (ValueError,TypeError) as error:self.reply(400,{'error':str(error)})
        except Exception:self.reply(503,{'error':'Запись не подтверждена. Повторите отправку.'})

def main():
    config=json.loads(Path(os.environ.get('HABITS_CONFIG','/opt/habits_api/config.json')).read_text())
    databases = config['databases']
    for name in ('habits_catalog', 'habits_log', 'tasks'):
        if not databases[name]:
            raise ValueError('Missing Notion database: ' + name)
    api_key = config['api_key']
    donation_api_key = config.get('donation_api_key')
    if not api_key or not config['notion_token'] or donation_api_key == api_key:
        raise ValueError('Configure a Notion token and distinct widget access keys.')
    store = Store(
        os.environ.get('HABITS_DB', '/var/lib/notion-habits/journal.sqlite3'),
        config.get('legacy_habit_aliases'),
    )
    store.configure_clock(config.get('tracker_timezone'))
    store.clock()
    notion = Notion(config['notion_token'], databases)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('HABITS_PORT', '8080'))), Handler)
    from tasks import TaskWorker
    server.store = store
    server.api_key = api_key
    server.donation_api_key = donation_api_key
    server.donation_data = Path(os.environ.get('DONATION_DATA', '/var/lib/notion-donation/data.json'))
    server.worker = Worker(store, notion)
    server.tasks = TaskWorker(store, notion)
    server.worker.start()
    server.tasks.start()
    server.serve_forever()
if __name__=='__main__':main()
