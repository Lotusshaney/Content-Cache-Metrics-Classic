#!/usr/bin/env python3
"""Receive contentcache_metrics.sh CSV via PUT; serve a dependency-free dashboard."""
import argparse
import csv
import hmac
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
FIELDS = '''bytesFromCacheToChild bytesFromCacheToClient bytesFromCacheToPeer bytesFromOriginToChild bytesFromOriginToClient bytesFromOriginToPeer bytesFromParentToChild bytesFromParentToClient bytesFromParentToPeer bytesFromPeerToChild bytesFromPeerToClient bytesDropped bytesImportedByHTTP bytesImportedByXPC bytesPurgedTotal bytesPurgedYoungerThan1Day bytesPurgedYoungerThan7Days bytesPurgedYoungerThan30Days importsByHTTP importsByXPC period requestsFromChild requestsFromClient requestsFromPeer requestsRejectedForNoSpace'''.split()
LOCK = threading.Lock()
DB = None
TZ = ZoneInfo('UTC')
PERIOD = 3600
ENROLL_TOKEN = ''  # empty disables token checks and approval
RETENTION_DAYS = 90  # 0 keeps data forever
RETENTION_CHOICES = (90, 180, 365, 0)
MAX_UPLOAD = 16 * 1024 * 1024


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS csv_servers (
        guid TEXT PRIMARY KEY, hostname TEXT NOT NULL, received INTEGER NOT NULL,
        source_ip TEXT, timezone TEXT, status TEXT NOT NULL DEFAULT 'approved');
        CREATE TABLE IF NOT EXISTS csv_metrics (
        guid TEXT NOT NULL, ts INTEGER NOT NULL, raw TEXT NOT NULL,
        PRIMARY KEY(guid, ts));
        CREATE INDEX IF NOT EXISTS csv_metrics_ts ON csv_metrics(ts);
        CREATE TABLE IF NOT EXISTS csv_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
    if 'status' not in [r[1] for r in db.execute('PRAGMA table_info(csv_servers)')]:
        db.execute("ALTER TABLE csv_servers ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
    db.commit()
    return db


def check_enrollment(guid, hostname, ip):
    """None if this server's upload may be ingested, else 'pending' or 'rejected'."""
    if not ENROLL_TOKEN:
        return None
    with LOCK, DB:
        row = DB.execute('SELECT status FROM csv_servers WHERE guid=?', (guid,)).fetchone()
        if row is None:
            DB.execute("INSERT INTO csv_servers (guid, hostname, received, source_ip, status) VALUES (?, ?, ?, ?, 'pending')",
                       (guid, hostname, int(time.time()), ip))
            return 'pending'
        if row['status'] == 'pending':
            DB.execute('UPDATE csv_servers SET hostname=?, received=?, source_ip=? WHERE guid=?',
                       (hostname, int(time.time()), ip, guid))
        return None if row['status'] == 'approved' else row['status']


def set_status(guid, status):
    with LOCK, DB:
        return DB.execute('UPDATE csv_servers SET status=? WHERE guid=?', (status, guid)).rowcount


def ingest_csv(body, guid, hostname, source_ip, timezone):
    if not guid or len(guid) > 200 or len(hostname) > 200:
        raise ValueError('Supply a server ID of 1–200 characters using ?server= or X-Server-ID')
    zone = ZoneInfo(timezone)
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig'), newline=''), strict=True)
    expected = ['timestamp'] + FIELDS
    if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('CSV must have a unique header row')
    missing = set(expected) - set(reader.fieldnames)
    if missing:
        raise ValueError('Missing CSV columns: ' + ', '.join(sorted(missing)))
    records = {}
    for line, row in enumerate(reader, 2):
        if None in row or any(row[k] is None for k in expected):
            raise ValueError(f'CSV row {line}: wrong number of columns')
        try:
            dt = datetime.fromisoformat(row['timestamp'])
            if dt.tzinfo is None:
                # Local timestamps cannot uniquely identify the repeated hour at DST end.
                dt = dt.replace(tzinfo=zone)
            ts = int(dt.timestamp())
            values = {}
            for k in FIELDS:
                v = Decimal(row[k])
                if not v.is_finite() or v < 0 or (k != 'period' and v != v.to_integral_value()):
                    raise ValueError(f'{k} must be a non-negative number' + ('' if k == 'period' else ' (whole number)'))
                values[k] = float(v) if k == 'period' else int(v)
            records[ts] = json.dumps(values)
        except (ValueError, OverflowError, InvalidOperation) as e:
            raise ValueError(f'CSV row {line}: {e}') from e
    now = int(time.time())
    with LOCK, DB:
        before = DB.total_changes
        DB.executemany('''INSERT INTO csv_metrics VALUES (?, ?, ?)
            ON CONFLICT(guid, ts) DO UPDATE SET raw=excluded.raw
            WHERE csv_metrics.raw != excluded.raw''', [(guid, ts, raw) for ts, raw in records.items()])
        changed = DB.total_changes - before
        DB.execute('''INSERT INTO csv_servers (guid, hostname, received, source_ip, timezone) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guid) DO UPDATE SET hostname=excluded.hostname,
            received=excluded.received, source_ip=excluded.source_ip, timezone=excluded.timezone''',
            (guid, hostname, now, source_ip, timezone))
    return {'ok': True, 'server': guid, 'rows': len(records), 'changed': changed}


def metrics(d):
    src = lambda s: sum(d.get('bytesFrom' + s + 'To' + dest, 0) for dest in ('Child', 'Client', 'Peer'))
    cache, origin, parent, peer = (src(s) for s in ('Cache', 'Origin', 'Parent', 'Peer'))
    pressure = next((p for k, p in [('requestsRejectedForNoSpace', 100), ('bytesPurgedYoungerThan1Day', 80), ('bytesPurgedYoungerThan7Days', 60), ('bytesPurgedYoungerThan30Days', 40), ('bytesPurgedTotal', 20)] if d[k] > 0), 0)
    dest = lambda t: sum(d.get('bytesFrom' + s + 'To' + t, 0) for s in ('Cache', 'Origin', 'Parent', 'Peer'))
    served = cache+origin+parent+peer
    period = d.get('period') or 0
    return dict(served=served, cache=cache, origin=origin, parent=parent, peer=peer,
        downloaded=origin+parent+peer, saved=max(0, served-(origin+parent+peer)),
        peak=served/period if period > 0 else 0, children=dest('Child'), peers=dest('Peer'), uploaded=d['bytesImportedByHTTP']+d['bytesImportedByXPC'],
        dropped=d['bytesDropped'], purged=d['bytesPurgedTotal'],
        requests=sum(d['requestsFrom'+s] for s in ('Child', 'Client', 'Peer')),
        rejected=d['requestsRejectedForNoSpace'], pressure=pressure,
        clients=dest('Client'))


def prune():
    if RETENTION_DAYS > 0:
        with LOCK, DB:
            DB.execute('DELETE FROM csv_metrics WHERE ts<?', (int(time.time()-RETENTION_DAYS*86400),))


def set_retention(days):
    global RETENTION_DAYS
    if days not in RETENTION_CHOICES:
        raise ValueError('retention_days must be one of 90, 180, 365 or 0 (forever)')
    with LOCK, DB:
        DB.execute('INSERT INTO csv_settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   ('retention_days', str(days)))
    RETENTION_DAYS = days
    prune()


def delete_all():
    with LOCK, DB:
        rows = DB.execute('SELECT COUNT(*) FROM csv_metrics').fetchone()[0]
        servers = DB.execute('SELECT COUNT(*) FROM csv_servers').fetchone()[0]
        DB.execute('DELETE FROM csv_metrics')
        DB.execute('DELETE FROM csv_servers')
    return {'ok': True, 'deleted_rows': rows, 'deleted_servers': servers}


def export_csv(qs):
    """Re-uploadable CSV (UTC timestamps); optional ?hours= and ?server= filters."""
    sql, args = 'SELECT guid, ts, raw FROM csv_metrics', []
    where = []
    if 'hours' in qs:
        where.append('ts>=?'); args.append(int(time.time() - parse_hours(qs, '24') * 3600))
    if qs.get('server', [''])[0]:
        where.append('guid=?'); args.append(qs['server'][0])
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    with LOCK:
        rows = DB.execute(sql + ' ORDER BY guid, ts', args).fetchall()
    out = io.StringIO(newline='')
    w = csv.writer(out, lineterminator='\n')
    w.writerow(['server', 'timestamp'] + FIELDS)
    for r in rows:
        d = json.loads(r['raw'])
        w.writerow([r['guid'], datetime.fromtimestamp(r['ts'], ZoneInfo('UTC')).isoformat()] + [d[k] for k in FIELDS])
    return out.getvalue().encode()


MAX_KEYS = ('pressure', 'peak')
BUCKETS = {'hour': 3600, '6h': 6*3600, 'day': 86400}


def parse_hours(qs, default):
    hours = float(qs.get('hours', [default])[0])
    if not math.isfinite(hours) or not 0 < hours <= 8760:
        raise ValueError('hours must be greater than 0 and at most 8760')
    return hours


def aggregate(hours, bucket=None):
    """Return (servers, history, bucket); history is empty when bucket is None."""
    now = int(time.time())
    since = now - hours * 3600
    with LOCK:
        servers = [dict(r) for r in DB.execute("SELECT * FROM csv_servers WHERE status='approved' ORDER BY hostname")]
        rows = DB.execute('SELECT * FROM csv_metrics WHERE ts>=? AND ts<=? ORDER BY ts', (since, now)).fetchall()
        latest = {r['guid']: r['ts'] for r in DB.execute('SELECT guid, MAX(ts) ts FROM csv_metrics GROUP BY guid')}
    groups = {}
    totals = {}
    for r in rows:
        m = metrics(json.loads(r['raw']))
        outs = [totals.setdefault(r['guid'], {})]
        if bucket:
            outs.append(groups.setdefault((r['guid'], r['ts']//bucket*bucket), {}))
        for out in outs:
            for k,v in m.items():
                out[k] = max(out.get(k, 0), v) if k in MAX_KEYS else out.get(k, 0) + v
            out['samples'] = out.get('samples', 0) + 1
    for s in servers:
        s.update(age=max(0, now-s['received']), online=now-s['received'] <= PERIOD*3+30,
                 latest=latest.get(s['guid']), totals=totals.get(s['guid'], {}))
    return servers, [dict(guid=g, t=t, **v) for (g,t),v in groups.items()], bucket


def dashboard(qs):
    hours = parse_hours(qs, '24')
    choice = qs.get('bucket', ['auto'])[0]
    if choice == 'auto':
        bucket = max(60, int(hours * 3600 / 500))
    elif choice in BUCKETS:
        bucket = BUCKETS[choice]
    else:
        raise ValueError('bucket must be auto, hour, 6h or day')
    servers, history, bucket = aggregate(hours, bucket)
    with LOCK:
        pending = [dict(r) for r in DB.execute("SELECT guid, hostname, received, source_ip, status FROM csv_servers WHERE status!='approved' ORDER BY received DESC")]
    return {'servers': servers, 'history': history, 'bucket': bucket, 'retention_days': RETENTION_DAYS, 'period': PERIOD,
            'pending': pending, 'enrollment': bool(ENROLL_TOKEN)}


def prometheus(qs):
    """Gauges over a trailing window (default 1h); not monotonic counters, since old rows are pruned."""
    hours = parse_hours(qs, '1')
    servers, _, _ = aggregate(hours)
    esc = lambda v: str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    spec = [
        ('ccm_server_up', 'gauge', '1 if the server uploaded recently', lambda s, t: int(s['online'])),
        ('ccm_last_upload_timestamp_seconds', 'gauge', 'Unix time of the last upload', lambda s, t: s['received']),
        ('ccm_last_sample_timestamp_seconds', 'gauge', 'Unix time of the newest sample', lambda s, t: s['latest'] or 0),
        ('ccm_samples', 'gauge', 'Samples in the window', lambda s, t: t.get('samples', 0)),
    ]
    for name, key, help_ in [
        ('served_bytes', 'served', 'Bytes served'), ('cache_bytes', 'cache', 'Bytes served from cache'),
        ('origin_bytes', 'origin', 'Bytes served from origin'), ('parent_bytes', 'parent', 'Bytes served from parent'),
        ('peer_bytes', 'peer', 'Bytes served from peers'), ('downloaded_bytes', 'downloaded', 'Bytes downloaded from upstream'),
        ('saved_bytes', 'saved', 'Bandwidth saved (served minus downloaded)'),
        ('clients_bytes', 'clients', 'Bytes served to clients'), ('children_bytes', 'children', 'Bytes served to child caches'),
        ('peers_bytes', 'peers', 'Bytes served to peer caches'), ('uploaded_bytes', 'uploaded', 'Bytes imported'),
        ('dropped_bytes', 'dropped', 'Bytes dropped'), ('purged_bytes', 'purged', 'Bytes purged'),
        ('requests', 'requests', 'Requests'), ('rejected_requests', 'rejected', 'Requests rejected for no space'),
        ('peak_throughput_bytes_per_second', 'peak', 'Peak per-sample throughput'),
        ('cache_pressure_percent', 'pressure', 'Maximum cache pressure')]:
        spec.append(('ccm_' + name, 'gauge', help_ + ' in the window', lambda s, t, key=key: t.get(key, 0)))
    out = []
    for name, kind, help_, f in spec:
        out += [f'# HELP {name} {help_}', f'# TYPE {name} {kind}']
        for s in servers:
            out.append(f'{name}{{server="{esc(s["guid"])}",hostname="{esc(s["hostname"])}"}} {f(s, s["totals"])}')
    return ('\n'.join(out) + '\n').encode()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, content_type='application/json', headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path == '/api/settings':
            return self.put_settings()
        if u.path in ('/api/approve', '/api/reject'):
            return self.review(u)
        if u.path not in ('/', '/api/metrics'):
            return self.send(404, {'error': 'not found'})
        try:
            if self.headers.get('Transfer-Encoding'):
                return self.send(400, {'error': 'Use Content-Length; chunked uploads are not supported'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_UPLOAD:
                return self.send(413, {'error': 'Upload must be between 1 byte and 16 MiB'})
            if self.headers.get('Content-Encoding', 'identity') != 'identity':
                return self.send(415, {'error': 'Upload uncompressed CSV'})
            if ENROLL_TOKEN:
                auth = self.headers.get('Authorization', '')
                if not auth.startswith('Bearer ') or not hmac.compare_digest(auth[7:].encode(), ENROLL_TOKEN.encode()):
                    return self.send(401, {'error': 'Missing or invalid enrollment token'})
            qs = parse_qs(u.query)
            guid = qs.get('server', [self.headers.get('X-Server-ID', '')])[0]
            hostname = self.headers.get('X-Hostname') or guid
            zone = qs.get('timezone', [self.headers.get('X-Timezone', str(TZ))])[0]
            self.connection.settimeout(30)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete upload')
            if not guid or len(guid) > 200 or len(hostname) > 200:
                raise ValueError('Supply a server ID of 1–200 characters using ?server= or X-Server-ID')
            state = check_enrollment(guid, hostname, self.client_address[0])
            if state:
                return self.send(403, {'error': f'Server {guid!r} is {state}; ' + ('approve it on the dashboard, then the next upload is accepted' if state == 'pending' else 'it was rejected on the dashboard')})
            self.send(200, ingest_csv(body, guid, hostname, self.client_address[0], zone))
        except (ValueError, UnicodeError, csv.Error, ZoneInfoNotFoundError) as e:
            self.send(400, {'error': str(e)})
        except Exception:
            self.log_error('CSV upload failed')
            self.send(500, {'error': 'Upload failed; check server logs'})

    def review(self, u):
        # The custom header forces a CORS preflight, so other websites cannot trigger this from a browser.
        if not self.headers.get('X-CCM-Action'):
            return self.send(400, {'error': 'Missing X-CCM-Action header'})
        guid = parse_qs(u.query).get('server', [''])[0]
        status = 'approved' if u.path == '/api/approve' else 'rejected'
        if not set_status(guid, status):
            return self.send(404, {'error': 'unknown server'})
        self.send(200, {'ok': True, 'server': guid, 'status': status})

    def put_settings(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                return self.send(413, {'error': 'Settings body must be between 1 and 4096 bytes'})
            self.connection.settimeout(30)
            data = json.loads(self.rfile.read(length))
            days = data.get('retention_days') if isinstance(data, dict) else None
            if data.get('retention_days') == 'forever':
                days = 0
            if not isinstance(days, int) or isinstance(days, bool):
                raise ValueError('retention_days must be one of 90, 180, 365 or 0 (forever)')
            set_retention(days)
            self.send(200, {'ok': True, 'retention_days': RETENTION_DAYS})
        except (ValueError, UnicodeError, AttributeError) as e:
            self.send(400, {'error': str(e)})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path != '/api/data':
            return self.send(404, {'error': 'not found'})
        if parse_qs(u.query).get('confirm') != ['delete-all']:
            return self.send(400, {'error': 'Add ?confirm=delete-all to delete all collected data'})
        self.send(200, delete_all())

    # The same CSV format and validation apply to POST and PUT.
    do_POST = do_PUT

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            return self.send(200, (HERE/'index.html').read_bytes(), 'text/html; charset=utf-8')
        if u.path == '/metrics':
            try:
                return self.send(200, prometheus(parse_qs(u.query)), 'text/plain; version=0.0.4; charset=utf-8')
            except ValueError as e:
                return self.send(400, {'error': str(e)})
        if u.path == '/api/export.csv':
            try:
                return self.send(200, export_csv(parse_qs(u.query)), 'text/csv; charset=utf-8',
                                 [('Content-Disposition', 'attachment; filename="contentcache_export.csv"')])
            except ValueError as e:
                return self.send(400, {'error': str(e)})
        if u.path == '/api/settings':
            return self.send(200, {'retention_days': RETENTION_DAYS})
        if u.path == '/api/themes':
            return self.send(200, list_themes(), headers=[('Cache-Control', 'no-store')])
        if u.path == '/api/dashboard':
            try:
                return self.send(200, dashboard(parse_qs(u.query)))
            except ValueError as e:
                return self.send(400, {'error': str(e)})
        self.send(404, {'error': 'not found'})


def list_themes():
    folder = HERE / 'themes'
    themes = []
    paths = sorted(folder.rglob('*.json'), key=lambda path: str(path).casefold())
    for path in paths:
        if not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
            continue
        try:
            theme = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(theme, dict) or theme.get('version') != 1:
            continue
        colours = theme.get('colours')
        if not isinstance(colours, dict) or not colours or any(
            not isinstance(value, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', value)
            for value in colours.values()
        ):
            continue
        relative = path.relative_to(folder)
        themes.append({
            'name': relative.stem.replace('-', ' ').replace('_', ' '),
            'folder': relative.parent.as_posix() if relative.parent.parts else '',
            'colours': colours,
        })
    return {'themes': themes}


def prune_loop():
    while True:
        time.sleep(3600)
        prune()


def main():
    global DB, TZ, PERIOD, RETENTION_DAYS, ENROLL_TOKEN
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--db', default=str(HERE/'ccmon.db'))
    ap.add_argument('--timezone', default='UTC', help='Time zone of CSV timestamps, e.g. Europe/London')
    ap.add_argument('--period', type=int, default=3600, help='Expected seconds between uploads')
    ap.add_argument('--enroll-token', default=os.environ.get('CCM_ENROLL_TOKEN', ''), help='Require this bearer token on uploads; new server IDs wait for approval on the dashboard (env: CCM_ENROLL_TOKEN)')
    ap.add_argument('--retention-days', type=int, default=90, help='Initial retention in days (0 = forever); a value saved from the dashboard takes precedence')
    a = ap.parse_args()
    if a.period <= 0 or a.retention_days < 0:
        ap.error('period must be positive and retention-days cannot be negative')
    try:
        TZ = ZoneInfo(a.timezone)
    except ZoneInfoNotFoundError:
        ap.error('Unknown time zone: '+a.timezone)
    PERIOD = a.period
    DB = connect(a.db)
    ENROLL_TOKEN = a.enroll_token
    if not ENROLL_TOKEN:
        with DB:
            DB.execute("UPDATE csv_servers SET status='approved'")
    RETENTION_DAYS = a.retention_days
    saved = DB.execute("SELECT value FROM csv_settings WHERE key='retention_days'").fetchone()
    if saved:
        RETENTION_DAYS = int(saved[0])
    prune()
    threading.Thread(target=prune_loop, daemon=True).start()
    print(f'Listening on http://{a.host}:{a.port}', flush=True)
    ThreadingHTTPServer((a.host,a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
