import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'load/results'


def percentile(values, p):
    data = sorted(values)
    if not data: return 0
    position = (len(data)-1)*p
    low, high = math.floor(position), math.ceil(position)
    return data[low] + (data[high]-data[low])*(position-low)


def write_csv(path, rows):
    if not rows: return
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


def analyze_stage(result):
    metrics = defaultdict(lambda: defaultdict(list))
    status = Counter()
    with gzip.open(RESULTS/(result['name']+'_raw.json.gz'), 'rt', encoding='utf-8') as f:
        for line in f:
            point = json.loads(line)
            if point['type'] != 'Point': continue
            metric, data = point['metric'], point['data']
            operation = (data.get('tags') or {}).get('operation')
            if metric in ('load_duration','load_failed','sql_duration','sql_commands'):
                metrics[operation][metric].append(data['value'])
            if metric == 'load_status': status[(operation,data['tags']['status'])] += int(data['value'])
    operations=[]
    durations=[]
    failures=[]
    for operation, values in metrics.items():
        ds=values['load_duration']; errors=values['load_failed']; durations.extend(ds); failures.extend(errors)
        operations.append({'operation':operation,'count':len(ds),'p50':percentile(ds,.5),'p95':percentile(ds,.95),'max':max(ds),'http_avg':sum(ds)/len(ds),'sql_avg':sum(values['sql_duration'])/max(1,len(values['sql_duration'])),'sql_count_avg':sum(values['sql_commands'])/max(1,len(values['sql_commands'])),'errors':sum(errors)})
    assert len(durations)==result['requests'], result['name']
    assert abs(percentile(durations,.95)-result['p95'])<.001, result['name']
    assert abs(sum(failures)/len(failures)-result['error_rate'])<.00001, result['name']
    with (RESULTS/(result['name']+'_resources.csv')).open(encoding='utf-8') as f:
        resources=[{k: float(v) for k,v in row.items() if k!='time_utc'} for row in csv.DictReader(f)]
    central=[r for r in resources if 5 <= r['elapsed_s'] <= result['duration']-1]
    resource={}
    for field in ('app_cpu','pg_cpu','app_rss_mb','pg_rss_mb','connections','active_connections','system_cpu','available_mb'):
        values=[row[field] for row in central]
        resource[field]={'median':percentile(values,.5),'p95':percentile(values,.95),'max':max(values,default=0)}
    status_rows=[{'operation':op,'status':code,'count':count} for (op,code),count in sorted(status.items())]
    write_csv(RESULTS/(result['name']+'_operations.csv'),sorted(operations,key=lambda x:x['operation']))
    return {'name':result['name'],'operations':operations,'statuses':status_rows,'resources':resource}


def analyze_profile(path):
    data=json.loads(path.read_text(encoding='utf-8'))
    frames=data['shared']['frames']
    total=0; handler_total=0
    inclusive=Counter(); self_time=Counter(); handler_inclusive=Counter()
    for profile in data['profiles']:
        weights=profile.get('weights') or [1]*len(profile['samples'])
        for stack,weight in zip(profile['samples'],weights):
            total+=weight
            in_handler=any(frames[i].get('file','').replace('\\','/').endswith('app/main.py') for i in stack)
            if in_handler: handler_total+=weight
            for index in set(stack):
                inclusive[index]+=weight
                if in_handler: handler_inclusive[index]+=weight
            if stack:self_time[stack[-1]]+=weight
    rows=[]
    for index,weight in handler_inclusive.most_common():
        frame=frames[index]
        rows.append({'function':frame['name'],'file':frame.get('file',''),'line':frame.get('line',0),'all_samples_pct':100*inclusive[index]/total,'handler_samples_pct':100*weight/handler_total,'self_samples_pct':100*self_time[index]/total})
    write_csv(RESULTS/'profile_functions.csv',rows)
    value={'total_weight':total,'handler_weight':handler_total,'handler_all_pct':100*handler_total/total,'thread_profiles':len(data['profiles']),'functions':rows}
    (RESULTS/'profile_analysis.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value


def main():
    results=[json.loads(p.read_text(encoding='utf-8')) for p in RESULTS.glob('*_result.json')]
    results.sort(key=lambda r:(r['mode'],r['workers'],r['rate'],r['name']))
    analyses=[analyze_stage(result) for result in results]
    write_csv(RESULTS/'stages.csv',results)
    (RESULTS/'analysis.json').write_text(json.dumps(analyses,ensure_ascii=False,indent=2),encoding='utf-8')
    profiles=list(RESULTS.glob('*_profile.json'))
    profiles.sort(key=lambda p:p.name=='profile_profile.json')
    for path in profiles:analyze_profile(path)
    print('Verified raw results:',len(results),'stages; profiles:',len(profiles))


if __name__=='__main__':main()
