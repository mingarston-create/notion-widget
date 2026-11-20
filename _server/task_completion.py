"""Durable completion-date writes. Stores IDs/statuses/dates, not task content."""
import datetime as dt
import json
from tasks import date_label

def fields(row):
 p=row['properties']
 return (p.get('Статус',{}).get('status') or {}).get('name'),(p.get('Завершено',{}).get('date') or {}).get('start')
class CompletionTracker:
 def __init__(self,store,notion):
  self.store=store;self.notion=notion
  with store.connect() as c:
   c.executescript('CREATE TABLE IF NOT EXISTS task_completion_state(id TEXT PRIMARY KEY,status TEXT,ended TEXT); CREATE TABLE IF NOT EXISTS task_completion_pending(id TEXT PRIMARY KEY,target TEXT NOT NULL,previous TEXT,edited TEXT);')
 def save(self,row):
  status,ended=fields(row)
  with self.store.connect() as c:c.execute('INSERT OR REPLACE INTO task_completion_state VALUES (?,?,?)',(row['id'],status,ended))
 def baseline(self,rows):
  with self.store.connect() as c:
   for row in rows:c.execute('INSERT OR REPLACE INTO task_completion_state VALUES (?,?,?)',(row['id'],*fields(row)))
 def observe(self,row,clock,enabled):
  status,ended=fields(row)
  with self.store.connect() as c:
   old=c.execute('SELECT status,ended FROM task_completion_state WHERE id=?',(row['id'],)).fetchone()
   pending=c.execute('SELECT * FROM task_completion_pending WHERE id=?',(row['id'],)).fetchone()
   transitioned=old and old['status']!='Выполнено' and status=='Выполнено'
   newly_done=not old and status=='Выполнено' and row.get('created_time','')>=enabled
   if not pending and (transitioned or newly_done) and (not ended or (old and ended==old['ended'])):
    # Notion exposes last edit time, not a separate status-change timestamp.
    # Capture its effective day once; retries never substitute recovery time.
    edited=row.get('last_edited_time') or dt.datetime.now(dt.timezone.utc).isoformat()
    target=date_label(edited,clock['timezone'])
    c.execute('INSERT INTO task_completion_pending VALUES (?,?,?,?)',(row['id'],target,ended,edited))
    pending={'target':target,'previous':ended}
  if pending:
   fresh=self.notion.request('pages/'+row['id']);status,ended=fields(fresh)
   if status=='Выполнено' and ended==pending['previous'] and not fresh.get('archived') and not fresh.get('in_trash'):
    fresh=self.notion.request('pages/'+row['id'],{'properties':{'Завершено':{'date':{'start':pending['target']}}}},'PATCH')
   # Fresh manual dates and reopened/deleted tasks supersede pending writes.
   self.save(fresh)
   with self.store.connect() as c:c.execute('DELETE FROM task_completion_pending WHERE id=?',(row['id'],))
   return fresh
  self.save(row);return row
