// k6 run -e BASE=https://api.example -e KEY=<gateway key> -e RATE=20000 deploy/loadtest/k6-ingest.js
// Mix that matches a flood surge: 80% sensor telemetry, 15% public reads, 5% citizen reports.
import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE || 'http://localhost:8000';
const KEY = __ENV.KEY || 'bench';
const RATE = parseInt(__ENV.RATE || '20000');

export const options = {
  scenarios: {
    surge: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: '2m',
      preAllocatedVUs: 2000, maxVUs: 8000,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:telemetry}': ['p(95)<150'],
    'http_req_duration{kind:report}': ['p(95)<300'],
  },
};

export default function () {
  const r = Math.random();
  if (r < 0.80) {
    const n = Math.floor(Math.random() * 5000);
    const body = JSON.stringify({ gatewayId: `gw-${n % 40}`, observations: [
      { node: `K${n}`, seq: __ITER, up: __ITER * 2, mq2: 110 + Math.random() * 30, tempC: 30 + Math.random() * 4, mic: 40 } ] });
    const res = http.post(`${BASE}/api/v1/ingest/telemetry`, body,
      { headers: { 'Content-Type': 'application/json', 'X-Mesh-Gateway-Key': KEY }, tags: { kind: 'telemetry' } });
    check(res, { 'accepted or shed': (x) => x.status === 202 || x.status === 503 });
  } else if (r < 0.95) {
    const res = http.get(`${BASE}/health/live`, { tags: { kind: 'read' } });
    check(res, { 'ok': (x) => x.status === 200 });
  } else {
    const body = JSON.stringify({ wardId: 'W1', category: 'flooding', location: [73.85, 18.52],
      note: 'water entering houses', deviceId: `load-${__VU}` });
    const res = http.post(`${BASE}/api/v1/ingest/reports`, body,
      { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `${__VU}-${__ITER}` }, tags: { kind: 'report' } });
    check(res, { 'queued': (x) => x.status === 202 || x.status === 201 || x.status === 429 });
  }
}
