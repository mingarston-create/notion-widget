"""Read annual book counts; only aggregates leave the service."""
import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo


class BookCounts:
    def __init__(self, notion, source):
        self.notion = notion
        self.source = source
        self.lock = threading.Lock()
        self.cache = None

    def snapshot(self, zone):
        year = dt.datetime.now(ZoneInfo(zone)).year
        with self.lock:
            if self.cache and self.cache['year'] == year and time.time()-self.cache['updated_at'] < 120:
                return self.cache
            body = {'page_size': 100, 'filter': {'and': [
                {'property':'Тип','select':{'equals':'Книга'}},
                {'property':'Статус','select':{'equals':'Прочитано'}},
            ]}}
            seen, cursors = set(), set()
            total = count = undated = 0
            while True:
                result = self.notion.request('data_sources/'+self.source+'/query',body,version='2025-09-03')
                for row in result['results']:
                    if row['id'] in seen or row.get('archived') or row.get('in_trash'):continue
                    seen.add(row['id'])
                    total += 1
                    period = row['properties']['Просмотрено']['date'] or {}
                    value = period.get('end') or period.get('start')
                    if not value:
                        undated += 1
                    elif len(value)==10:
                        count += dt.date.fromisoformat(value).year == year
                    else:
                        count += dt.datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo(zone)).year == year
                if not result.get('has_more'):break
                cursor=result.get('next_cursor')
                if not cursor or cursor in cursors:raise ValueError('Invalid pagination')
                cursors.add(cursor);body['start_cursor']=cursor
            self.cache = dict(year=year, count=count, total=total, undated=undated, updated_at=time.time())
            return self.cache
