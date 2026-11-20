"""Compatibility entry point: the API now rolls dates forward without deleting history."""
import json
import urllib.request
from pathlib import Path
config=json.loads(Path('/etc/notion-habits/config.json').read_text())
request=urllib.request.Request('http://127.0.0.1:8080/api/habits/status',headers={'Authorization':'Bearer '+config['api_key']})
with urllib.request.urlopen(request,timeout=15) as response:state=json.load(response)
print(json.dumps({'today':state['today'],'pending_days':state['sync']['pending_days'],'destructive_reset':False}))
