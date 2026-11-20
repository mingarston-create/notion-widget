"""Task metrics and durable completion-date synchronization; never stores task contents."""
import datetime as dt
import time
import threading
from zoneinfo import ZoneInfo
def date_label(value,zone):
    if not value:return None
    if len(value)==10:return value
    instant=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    local=instant.astimezone(ZoneInfo(zone))
    return (local.date()-dt.timedelta(days=local.hour<3)).isoformat()
def calculate(rows,day,zone):
    counts=dict(inbox=0,urgent=0,active=0,pool=0,done=0)
    seen=set()
    for row in rows:
        if row['id'] in seen:continue
        seen.add(row['id']);p=row['properties']
        if row.get('archived') or row.get('in_trash'):continue
        status=(p.get('Статус',{}).get('status') or {}).get('name')
        if status=='Выполнено':
            ended=(p.get('Завершено',{}).get('date') or {}).get('start')
            counts['done']+=date_label(ended,zone)==day
            continue
        if status not in ('Входящее','К выполнению','В работе'):continue
        counts['active']+=status=='В работе';counts['pool']+=status=='К выполнению'
        counts['inbox']+=status=='Входящее'
        due=p.get('Срок',{}).get('date') or {}
        due_day=date_label(due.get('end') or due.get('start'),zone)
        importance=(p.get('Важность',{}).get('select') or {}).get('name')
        counts['urgent']+=importance=='Высокая' or bool(due_day and due_day<=day)
    counts['todayTotal']=counts['urgent']+counts['done']
    counts['pct']=int(100*counts['done']/counts['todayTotal']+.5) if counts['todayTotal'] else 0
    return counts
class TaskWorker(threading.Thread):
    def __init__(self,store,notion):
        super().__init__(daemon=True);self.store=store;self.notion=notion;self.stop=threading.Event()
    def refresh(self):
        clock=self.store.clock();day=self.store.day()
        from task_completion import CompletionTracker
        tracker=CompletionTracker(self.store,self.notion)
        started=dt.datetime.now(dt.timezone.utc).isoformat()
        enabled=self.store.meta('task_completion_enabled')
        if not enabled:
            tracker.baseline(self.notion.query(self.notion.tasks_id))
            self.store.set_meta('task_completion_enabled',started)
            self.store.set_meta('task_completion_cursor',started)
            enabled=started
        cursor=self.store.meta('task_completion_cursor',enabled)
        cursor=(dt.datetime.fromisoformat(cursor)-dt.timedelta(seconds=60)).isoformat()
        lower=(dt.date.fromisoformat(day)-dt.timedelta(days=2)).isoformat()
        rows=self.notion.query(self.notion.tasks_id,{'filter':{'or':[
            {'property':'Статус','status':{'does_not_equal':'Выполнено'}},
            {'property':'Завершено','date':{'is_empty':True}},
            {'property':'Завершено','date':{'on_or_after':lower}},
            {'timestamp':'last_edited_time','last_edited_time':{'on_or_after':cursor}}
        ]}})
        by_id={r['id']:r for r in rows}
        with self.store.connect() as c:pending=[r['id'] for r in c.execute('SELECT id FROM task_completion_pending')]
        for key in pending:
            if key not in by_id:by_id[key]=self.notion.request('pages/'+key)
        rows=[tracker.observe(row,clock,enabled) for row in by_id.values()]
        self.store.set_meta('task_completion_cursor',started)
        if clock['revision']!=self.store.clock()['revision'] or day!=self.store.day():return False
        result={'metrics':calculate(rows,day,clock['timezone']),'today':day,'timezone':clock['timezone'],'clock_revision':clock['revision'],'updated_at':time.time()}
        self.store.set_meta('task_snapshot',result);self.store.set_meta('task_sync_error',None);return True
    def snapshot(self):
        data=self.store.meta('task_snapshot');clock=self.store.clock()
        stale=not data or time.time()-data['updated_at']>90 or data['today']!=self.store.day() or data['clock_revision']!=clock['revision']
        return {'data':data,'sync':{'initialized':bool(data),'stale':stale,'error':self.store.meta('task_sync_error')},'generated_at':time.time()}
    def run(self):
        while not self.stop.is_set():
            try:self.refresh()
            except Exception:self.store.set_meta('task_sync_error','Не удалось обновить данные из Notion. Показана последняя сохранённая версия.')
            self.stop.wait(10)
