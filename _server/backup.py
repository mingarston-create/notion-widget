"""Consistent SQLite snapshots; no token or server restart required."""
import datetime,os,sqlite3
from pathlib import Path
root=Path('/var/lib/notion-habits');target=root/'backups';target.mkdir(mode=0o700,exist_ok=True)
filename=target/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite3')
with sqlite3.connect(root/'journal.sqlite3') as source,sqlite3.connect(filename) as destination:source.backup(destination)
os.chmod(filename,0o600)
for old in sorted(target.glob('*.sqlite3'))[:-30]:old.unlink()
