import http from 'k6/http';
import encoding from 'k6/encoding';
import exec from 'k6/execution';
import { Trend, Rate, Counter } from 'k6/metrics';

const rate = Number(__ENV.RATE || 2);
const duration = __ENV.DURATION || '30s';
const mode = __ENV.MODE || 'mixed';
const base = __ENV.BASE_URL || 'http://127.0.0.1:8020';
const fixtures = JSON.parse(open(__ENV.FIXTURES || '../.runtime/fixtures.json'));
const headers = { Authorization: 'Basic ' + encoding.b64encode((__ENV.APP_USERNAME || 'demo') + ':' + (__ENV.APP_PASSWORD || 'demo123')), 'Content-Type': 'application/json' };
const latency = new Trend('load_duration', true);
const failures = new Rate('load_failed');
const requests = new Counter('load_requests');
const sqlTime = new Trend('sql_duration', true);
const sqlCount = new Trend('sql_commands');
const statuses = new Counter('load_status');
const normal = http.expectedStatuses(200);
const created = http.expectedStatuses(201, 409);
const cancelled = http.expectedStatuses(200, 409);
const schedule = ['list','card','slots','specialists','list','card','services','list','slots','summary','list','card','specialists','list','services','create','list','slots','card','cancel'];

export const options = {
  scenarios: { [mode]: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration, preAllocatedVUs: Number(__ENV.VUS || 128), maxVUs: Number(__ENV.VUS || 128), gracefulStop: '10s' } },
  thresholds: { load_duration: ['p(95)<500'], load_failed: ['rate<0.01'] },
  setupTimeout: '15s',
  discardResponseBodies: true,
  summaryTrendStats: ['avg','med','p(95)','max'],
};

export function setup() {
  const response = http.get(base + '/api/services', { headers, tags: { scope: 'setup' }, timeout: '10s', responseCallback: normal });
  if (response.status !== 200) throw new Error('Authentication/setup failed: HTTP ' + response.status);
}

export default function () {
  const index = exec.scenario.iterationInTest;
  const operation = mode === 'heavy' ? 'summary' : schedule[index % schedule.length];
  const number = Math.floor(index / schedule.length);
  let path, body = null, method = 'GET', callback = normal;
  switch (operation) {
    case 'list': path = '/api/appointments?page=1&size=20'; break;
    case 'card': path = '/api/appointments/' + fixtures.card_ids[index % fixtures.card_ids.length]; break;
    case 'specialists': path = '/api/specialists'; break;
    case 'services': path = '/api/services'; break;
    case 'slots': path = '/api/slots?service_id=1&date=2026-10-12&limit=100'; break;
    case 'summary': path = '/api/summary?start_date=2026-10-12&end_date=2026-11-11'; break;
    case 'create':
      path = '/api/appointments'; method = 'POST'; callback = created;
      body = JSON.stringify({slot_id: fixtures.free_slot_ids[number % fixtures.free_slot_ids.length], service_id: 1, client_name: 'ilya_safronov load ' + index});
      break;
    case 'cancel':
      path = '/api/appointments/' + fixtures.booked_ids[number % fixtures.booked_ids.length] + '/cancel'; method = 'POST'; callback = cancelled;
      break;
  }
  const response = http.request(method, base + path, body, { headers, tags: { scope: 'load', operation, name: operation }, timeout: '10s', responseCallback: callback });
  const expected = operation === 'create' ? [201,409] : operation === 'cancel' ? [200,409] : [200];
  const failed = !expected.includes(response.status);
  const tags = { operation, status: String(response.status) };
  latency.add(response.timings.duration, tags);
  failures.add(failed, tags);
  requests.add(1, tags);
  statuses.add(1, tags);
  if (response.headers['X-Sql-Time-Ms'] !== undefined) sqlTime.add(Number(response.headers['X-Sql-Time-Ms']), {operation});
  if (response.headers['X-Sql-Count'] !== undefined) sqlCount.add(Number(response.headers['X-Sql-Count']), {operation});
  if (failed) console.error(JSON.stringify({operation, status: response.status, error: response.error || '', url: path}));
}

export function handleSummary(data) {
  return { [__ENV.SUMMARY || 'load/results/summary.json']: JSON.stringify(data, null, 2) };
}
