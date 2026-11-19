import argparse
import csv
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.tools/python'))
sys.path.insert(0, str(ROOT))
import psutil
import psycopg
import requests
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

SETTINGS = {k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None}
ENV = os.environ | SETTINGS | {'PYTHONIOENCODING': 'utf-8'}
URL = make_url(SETTINGS['DATABASE_URL'])
SCHEMA = SETTINGS.get('DB_SCHEMA', 'ilya_safronov')
PORT = int(SETTINGS.get('APP_PORT', '8020'))
DSN = URL.set(drivername='postgresql').render_as_string(hide_password=False)
K6 = ROOT / '.tools/k6.exe' if (ROOT / '.tools/k6.exe').exists() else Path(shutil.which('k6') or 'k6')
SPY = ROOT / '.tools/python/bin/py-spy.exe' if (ROOT / '.tools/python/bin/py-spy.exe').exists() else Path(sys.executable).with_name('py-spy.exe')
RESULTS = ROOT / 'load/results'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def prepare():
    if not (URL.database or '').startswith('ilya_safronov_hw2'):
        raise ValueError('This runner only resets the dedicated ilya_safronov_hw2 database')
    admin = URL.set(drivername='postgresql', database='postgres').render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as c:
        if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s', (URL.database,)).fetchone():
            c.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(URL.database)))
    subprocess.run([sys.executable, '-m', 'scripts.seed', '--size', 'large'], cwd=ROOT, env=ENV, check=True, stdout=subprocess.DEVNULL)
    with psycopg.connect(DSN, autocommit=True) as c:
        fixtures = {
            'card_ids': [r[0] for r in c.execute(f'SELECT id FROM "{SCHEMA}".appointments ORDER BY id LIMIT 100')],
            'booked_ids': [r[0] for r in c.execute(f'SELECT id FROM "{SCHEMA}".appointments WHERE status=\'booked\' ORDER BY id LIMIT 5000')],
            'free_slot_ids': [r[0] for r in c.execute(f'SELECT s.id FROM "{SCHEMA}".slots s WHERE NOT EXISTS (SELECT 1 FROM "{SCHEMA}".appointments a WHERE a.slot_id=s.id AND a.status=\'booked\') ORDER BY s.id LIMIT 5000')],
        }
    save(ROOT / '.runtime/fixtures.json', fixtures)


def start_server(workers):
    log = (RESULTS / f'service_{workers}workers.log').open('a', encoding='utf-8')
    p = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(PORT), '--workers', str(workers), '--no-access-log'], cwd=ROOT, env=ENV, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        if p.poll() is not None: raise RuntimeError('Server failed; see service log')
        try:
            if requests.get(f'http://127.0.0.1:{PORT}/health', timeout=1).status_code == 200: return p
        except requests.RequestException: pass
        time.sleep(.2)
    raise RuntimeError('Server startup timed out')


def stop_server(server):
    try:
        p = psutil.Process(server.pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try: child.terminate()
            except psutil.Error: pass
        psutil.wait_procs([p] + children, timeout=5)
    except psutil.Error: pass


class Monitor:
    def __init__(self, server_pid):
        self.server_pid = server_pid
        self.cache = {}

    def sample(self):
        try: app_ids = {self.server_pid} | {p.pid for p in psutil.Process(self.server_pid).children(recursive=True)}
        except psutil.Error: app_ids = set()
        groups = {'app': [0., 0], 'postgres': [0., 0]}
        for p in psutil.process_iter(['pid','name']):
            group = 'app' if p.pid in app_ids else 'postgres' if p.info['name'].lower() == 'postgres.exe' else None
            if group:
                try:
                    proc = self.cache.setdefault(p.pid, p)
                    groups[group][0] += proc.cpu_percent(None)
                    groups[group][1] += proc.memory_info().rss
                except psutil.Error: pass
        return {'app_cpu': groups['app'][0], 'pg_cpu': groups['postgres'][0], 'app_rss_mb': groups['app'][1]/1048576, 'pg_rss_mb': groups['postgres'][1]/1048576, 'system_cpu': psutil.cpu_percent(None), 'available_mb': psutil.virtual_memory().available/1048576}


def wait_idle(monitor):
    stable = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        sample = monitor.sample()
        stable = stable + 1 if sample['app_cpu'] + sample['pg_cpu'] < 30 else 0
        if stable >= 4: return
        time.sleep(1)
    raise RuntimeError('CPU did not return to idle between stages')


def monitor_loop(monitor, stop, path, stage_start):
    with psycopg.connect(DSN, autocommit=True) as c, path.open('w', newline='', encoding='utf-8') as stream:
        fields = ['time_utc','elapsed_s','app_cpu','pg_cpu','app_rss_mb','pg_rss_mb','system_cpu','available_mb','connections','active_connections']
        writer = csv.DictWriter(stream, fields); writer.writeheader()
        while not stop.is_set():
            sample = monitor.sample()
            total, active = c.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE state='active') FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'").fetchone()
            writer.writerow({'time_utc': datetime.now(timezone.utc).isoformat(), 'elapsed_s': round(time.monotonic()-stage_start,3), **sample, 'connections': total, 'active_connections': active})
            stream.flush()
            stop.wait(1)


def stage(server, mode, rate, duration=30, name=None, profile=False, workers=1):
    name = name or f'{mode}_{rate}'
    print(f'PREPARE {name}', flush=True)
    prepare()
    monitor = Monitor(server.pid)
    wait_idle(monitor)
    env = ENV | {'MODE': mode, 'RATE': str(rate), 'DURATION': f'{duration}s', 'BASE_URL': f'http://127.0.0.1:{PORT}', 'FIXTURES': str(ROOT/'.runtime/fixtures.json'), 'SUMMARY': str(RESULTS/f'{name}_summary.json'), 'K6_NO_USAGE_REPORT': 'true'}
    output = (RESULTS/f'{name}_k6.txt').open('w', encoding='utf-8')
    stop = threading.Event()
    started = time.monotonic()
    thread = threading.Thread(target=monitor_loop, args=(monitor, stop, RESULTS/f'{name}_resources.csv', started)); thread.start()
    command = [str(K6), 'run', '--quiet', '--out', 'json='+str(RESULTS/f'{name}_raw.json.gz'), str(ROOT/'load/test.js')]
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    spy = None
    if profile:
        time.sleep(4)
        spy_log = (RESULTS/f'{name}_pyspy.txt').open('w', encoding='utf-8')
        spy = subprocess.Popen([str(SPY),'record','--pid',str(server.pid),'--duration','20','--rate','100','--idle','--threads','--format','speedscope','--output',str(RESULTS/f'{name}_profile.json')], cwd=ROOT, stdout=spy_log, stderr=subprocess.STDOUT)
    returncode = proc.wait(timeout=duration+45)
    stop.set(); thread.join(timeout=10); output.close()
    if spy is not None:
        if spy.wait(timeout=20) != 0: raise RuntimeError('py-spy failed; see profiler log')
        spy_log.close()
    summary = json.loads((RESULTS/f'{name}_summary.json').read_text(encoding='utf-8'))
    metric = summary['metrics']
    duration_values = metric['load_duration']['values']
    result = {'name':name,'mode':mode,'rate':rate,'duration':duration,'workers':workers,'exit_code':returncode,'p50':duration_values['med'],'p95':duration_values['p(95)'],'max':duration_values['max'],'error_rate':metric['load_failed']['values']['rate'],'requests':metric['load_requests']['values']['count'],'dropped':metric.get('dropped_iterations',{}).get('values',{}).get('count',0),'run_duration_s':summary['state']['testRunDurationMs']/1000}
    result['actual_rps'] = result['requests']/result['run_duration_s']
    result['passed'] = result['p95'] < 500 and result['error_rate'] < .01
    save(RESULTS/f'{name}_result.json', result)
    print('RESULT '+json.dumps(result), flush=True)
    wait_idle(monitor)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['prepare','smoke','mixed','heavy','profile','tuning','all'], default='all')
    parser.add_argument('--rate', type=int, default=2)
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True, parents=True)
    if args.mode == 'prepare': prepare(); return
    server = start_server(2 if args.mode == 'tuning' else 1)
    try:
        if args.mode == 'smoke': stage(server,'mixed',2,5,'smoke'); return
        if args.mode in ('profile','tuning'):
            stage(server,'heavy',args.rate,30,args.mode,profile=args.mode=='profile',workers=2 if args.mode=='tuning' else 1); return
        results=[]
        for mode in (['mixed','heavy'] if args.mode=='all' else [args.mode]):
            for rate in [1,2,4,8,16,32,64,128,256,512]:
                result=stage(server,mode,rate)
                results.append(result)
                save(RESULTS/'stages.json',results)
                if not result['passed']: break
    finally: stop_server(server)


if __name__ == '__main__': main()
