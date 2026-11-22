import datetime as dt
import importlib.util
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '_server'))
from service import Store, HabitConflict, reconcile_log, sync_projection, DAYS, effective_date, Handler
from tasks import TaskWorker
from books import BookCounts
from types import SimpleNamespace

class JournalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(Path(tmp.name)/'journal.sqlite3')
        self.store.configure_clock('Europe/Moscow')
        self.store.set_meta('initialized', True)
        self.day=self.store.day()
        self.rows=[{'id':h,'properties':{'Привычка':{'title':[{'plain_text':h}]}}} for h in ('first','second')]
        self.store.refresh_catalog(self.rows)
        self.store.snapshot()

    def op(self, name, done, revision=0, key='first', **extra):
        return dict(operation_id=name.ljust(20,'-'),id=key,date=self.day,done=done,
                    journal_id=self.store.journal_id,base_revision=revision,**extra)

    def test_offline_replay_and_two_devices(self):
        a=self.op('phone',True)
        self.store.accept(self.op('desktop-set',True))
        self.store.accept(self.op('desktop-clear',False,1))
        with self.assertRaises(HabitConflict):self.store.accept(a)
        self.store.accept(self.op('independent',True,key='second'))
        a['base_revision']=3
        self.assertTrue(self.store.accept(a)['accepted'])
        self.assertTrue(self.store.accept(a)['duplicate'])
        self.assertEqual(self.store.snapshot()['revision'],4)
        self.assertEqual(set(self.store.snapshot()['completed_today']),{'first','second'})

    def test_offline_chain_and_restored_journal(self):
        a=self.op('first-offline',True)
        self.store.accept(a)
        self.store.accept(self.op('second-offline',False,previous_operation=a['operation_id']))
        with self.assertRaises(HabitConflict):
            self.store.accept(self.op('wrong-lineage',True,revision=2,base_operation='missing-operation'))
        self.assertEqual(self.store.snapshot()['revision'],2)

    def test_manual_log_edit_then_checkbox(self):
        with self.store.connect() as db:db.execute("UPDATE days SET page='log',synced=version")
        page={'id':'log','last_edited_time':'m1','properties':{'Дата':{'date':{'start':self.day}},'Привычки':{'multi_select':[]}}}
        self.assertFalse(reconcile_log(self.store,page))
        page['properties']['Привычки']['multi_select']=[{'name':'🌱 first'}]
        self.assertTrue(reconcile_log(self.store,page))
        self.assertFalse(reconcile_log(self.store,page))
        self.assertEqual(self.store.snapshot()['completed_today'],['first'])
        col=DAYS[dt.date.fromisoformat(self.day).weekday()]
        row=self.rows[0];row['properties'][col]={'checkbox':True}
        api=SimpleNamespace(request=lambda *args:self.fail('Unexpected outgoing patch'))
        sync_projection(self.store,api,[row])
        row['properties'][col]['checkbox']=False;row['last_edited_time']='m2'
        sync_projection(self.store,api,[row])
        self.assertEqual(self.store.snapshot()['completed_today'],[])

    def test_server_scope_and_midnight(self):
        codes=[]
        class Fake:
            server=SimpleNamespace(store=self.store,api_key='mock')
            path='/api/habits/status?scope=week'
            def authorized(self,k):return True
            def reply(self,c,d):codes.append((c,d))
        Handler.do_GET(Fake())
        self.assertEqual(codes[0][1]['scope'],'week')
        local=lambda t:effective_date(dt.datetime.fromisoformat(t),'Europe/Moscow')
        self.assertEqual(local('2026-11-21T23:59:59+00:00'),'2026-11-21')
        self.assertEqual(local('2026-11-22T00:00:00+00:00'),'2026-11-22')

    def test_task_increment_keeps_untouched_metrics(self):
        def task(key,status):
            return {'id':key,'properties':{'Статус':{'status':{'name':status}},'Завершено':{'date':None}}}
        rows=[task('a','В работе'),task('b','Входящее')]
        class API:
            tasks_id='tasks'
            def query(self,source,body=None):
                return [task('b','К выполнению')] if body else rows
        w=TaskWorker(self.store,API());w.refresh();w.refresh()
        metrics=w.snapshot()['data']['metrics']
        self.assertEqual((metrics['active'],metrics['inbox'],metrics['pool']),(1,0,1))

    def test_books_with_missing_dates(self):
        year=dt.datetime.now(dt.timezone.utc).year
        rows=[{'id':str(i),'properties':{'Просмотрено':{'date':v}}} for i,v in enumerate(
            [None,{'start':f'{year}-04-01'},{'start':f'{year-1}-12-31T22:00:00Z'}])]
        api=SimpleNamespace(request=lambda *a,**k:{'results':rows,'has_more':False})
        result=BookCounts(api,'demo').snapshot('Europe/Moscow')
        self.assertEqual((result['count'],result['total'],result['undated']),(2,3,1))

    def test_donation_civil_date(self):
        spec=importlib.util.spec_from_file_location('donation',ROOT/'_server/donation/sync.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        opts=dict(blood_option='b',plasma_option='p',kind_property='kind',date_property='date',free_property='free')
        row={'id':'r','properties':{'kind':{'id':'kind','select':{'id':'b'}},'date':{'id':'date','date':{'start':'2026-11-21T22:00:00Z'}},'free':{'id':'free','checkbox':True}}}
        now=dt.datetime.fromisoformat('2026-11-21T23:00:00+00:00')
        self.assertEqual(mod.aggregate([row],now,opts)['lastDonation'],'2026-11-22')
        row['properties']['date']['date']['start']='2026-11-21'
        self.assertEqual(mod.aggregate([row],now,opts)['lastDonation'],'2026-11-21')

if __name__=='__main__':unittest.main()
