import os
import sys
import json
import uuid
import time as time_module
import subprocess
import threading
import shutil
import re
import csv
import io
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, Response

try:
    from flasgger import Swagger, swag_from
    HAS_SWAGGER = True
except ImportError:
    HAS_SWAGGER = False

BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
RESULTS_DIR = BASE_DIR / "results"
HISTORY_DIR = BASE_DIR / "history"
RESPONSES_DIR = BASE_DIR / "responses"
SCHEDULES_FILE = BASE_DIR / "schedules.json"
WEBHOOKS_FILE = BASE_DIR / "webhooks.json"
K6_EXE = os.environ.get("K6_PATH", "C:\\Program Files\\k6\\k6.exe")
MAX_CONCURRENT_TESTS = int(os.environ.get("MAX_CONCURRENT_TESTS", "10"))
MAX_SCRIPT_AGE_DAYS = int(os.environ.get("MAX_SCRIPT_AGE_DAYS", "7"))

SCRIPTS_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)
RESPONSES_DIR.mkdir(exist_ok=True)

active_tests = {}
cancel_events = {}
comparisons = {}
active_test_count = 0
active_test_lock = threading.Lock()
schedules = []
webhooks = []
scheduler_running = False

K6_LOGIN_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<5000'],
    http_req_failed: ['rate<0.1'],
  },
  noConnectionReuse: true,
};

const BASE_URL = `__TARGET_URL__`;
const LOGIN_ENDPOINT = `__LOGIN_ENDPOINT__`;

export default function () {
  const payload = JSON.stringify({
    __USERNAME_FIELD__: `__USERNAME__`,
    __PASSWORD_FIELD__: `__PASSWORD__`,
  });
  const params = {
    headers: __HEADERS__,
    tags: { test_type: 'login' },
  };
  const res = http.post(`${BASE_URL}${LOGIN_ENDPOINT}`, payload, params);
  check(res, {
    'login status 2xx': (r) => r.status >= 200 && r.status < 300,
    'login response time ok': (r) => r.timings.duration < 3000,
  });
  if (res.status >= 400) {
    console.log(`FAIL: ${res.status} ${res.body}`);
  }
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_UPLOAD_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<10000'],
    http_req_failed: ['rate<0.1'],
  },
  noConnectionReuse: true,
};

const BASE_URL = `__TARGET_URL__`;
const UPLOAD_ENDPOINT = `__UPLOAD_ENDPOINT__`;
const FILE_SIZE = __FILE_SIZE__;
const METHOD = `__UPLOAD_METHOD__`;

export default function () {
  const fileName = `test-${__VU}-${__ITER}.bin`;
  const fileContent = new Uint8Array(FILE_SIZE);
  for (let i = 0; i < FILE_SIZE; i++) {
    fileContent[i] = Math.floor(Math.random() * 256);
  }

  const blob = http.file(fileContent, fileName, 'application/octet-stream');
  const uploadPayload = { '__FILE_FIELD__': blob };

  const params = {
    headers: __HEADERS__,
    tags: { test_type: 'upload' },
  };

  let res;
  if (METHOD === 'POST') {
    res = http.post(`${BASE_URL}${UPLOAD_ENDPOINT}`, uploadPayload, params);
  } else {
    res = http.put(`${BASE_URL}${UPLOAD_ENDPOINT}`, uploadPayload, params);
  }
  check(res, {
    'upload status 2xx': (r) => r.status >= 200 && r.status < 300,
    'upload completed': (r) => r.timings.duration < 8000,
  });
  if (res.status >= 400) {
    console.log(`FAIL: ${res.status} ${res.body}`);
  }
  sleep(Math.random() * 1 + 0.3);
}
'''

K6_COMBINED_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<5000'],
    http_req_failed: ['rate<0.1'],
  },
  noConnectionReuse: true,
};

const BASE_URL = `__TARGET_URL__`;
const LOGIN_ENDPOINT = `__LOGIN_ENDPOINT__`;
const UPLOAD_ENDPOINT = `__UPLOAD_ENDPOINT__`;
const FILE_SIZE = __FILE_SIZE__;
const UPLOAD_METHOD = `__UPLOAD_METHOD__`;

export default function () {
  const payload = JSON.stringify({
    __USERNAME_FIELD__: `__USERNAME__`,
    __PASSWORD_FIELD__: `__PASSWORD__`,
  });
  const loginParams = {
    headers: __LOGIN_HEADERS__,
    tags: { test_type: 'login' },
  };
  const loginRes = http.post(`${BASE_URL}${LOGIN_ENDPOINT}`, payload, loginParams);
  check(loginRes, {
    'login status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  if (loginRes.status >= 400) {
    console.log(`FAIL LOGIN: ${loginRes.status} ${loginRes.body}`);
  }

  const fileName = `upload-${__VU}-${__ITER}.bin`;
  const fileContent = new Uint8Array(FILE_SIZE);
  for (let i = 0; i < FILE_SIZE; i++) {
    fileContent[i] = Math.floor(Math.random() * 256);
  }
  const blob = http.file(fileContent, fileName, 'application/octet-stream');
  const uploadPayload = { '__FILE_FIELD__': blob };
  const uploadParams = {
    headers: __UPLOAD_HEADERS__,
    tags: { test_type: 'upload' },
  };

  let uploadRes;
  if (UPLOAD_METHOD === 'POST') {
    uploadRes = http.post(`${BASE_URL}${UPLOAD_ENDPOINT}`, uploadPayload, uploadParams);
  } else {
    uploadRes = http.put(`${BASE_URL}${UPLOAD_ENDPOINT}`, uploadPayload, uploadParams);
  }
  check(uploadRes, {
    'upload status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  if (uploadRes.status >= 400) {
    console.log(`FAIL UPLOAD: ${uploadRes.status} ${uploadRes.body}`);
  }

  sleep(Math.random() * 2 + 0.5);
}
'''

K6_GENERIC_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: __THRESHOLDS__,
  noConnectionReuse: true,
};

const BASE_URL = `__TARGET_URL__`;
const REQUESTS = __REQUESTS__;

export default function () {
  const idx = (__VU + __ITER) % REQUESTS.length;
  const req = REQUESTS[idx];
  const url = `${BASE_URL}${req.path}`;
  const params = {
    headers: Object.assign({}, req.headers),
    tags: { test_type: 'generic', endpoint: req.path },
  };

  let res;
  switch (req.method) {
    case 'GET': res = http.get(url, params); break;
    case 'POST': res = http.post(url, req.body || '', params); break;
    case 'PUT': res = http.put(url, req.body || '', params); break;
    case 'PATCH': res = http.patch(url, req.body || '', params); break;
    case 'DELETE': res = http.del(url, null, params); break;
    default: res = http.get(url, params);
  }

  check(res, {
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  if (res.status >= 400) {
    console.log(`FAIL: ${req.method} ${url} ${res.status}`);
  }
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_HAR_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: __THRESHOLDS__,
  noConnectionReuse: true,
};

const ENTRIES = __HAR_ENTRIES__;

export default function () {
  const idx = (__VU + __ITER) % ENTRIES.length;
  const entry = ENTRIES[idx];
  const params = {
    headers: Object.assign({}, entry.headers),
    tags: { test_type: 'har', url: entry.url },
  };

  let res;
  switch (entry.method) {
    case 'GET': res = http.get(entry.url, params); break;
    case 'POST': res = http.post(entry.url, entry.body || '', params); break;
    case 'PUT': res = http.put(entry.url, entry.body || '', params); break;
    case 'PATCH': res = http.patch(entry.url, entry.body || '', params); break;
    case 'DELETE': res = http.del(entry.url, null, params); break;
    default: res = http.get(entry.url, params);
  }

  check(res, {
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  if (res.status >= 400) {
    console.log(`FAIL: ${entry.method} ${entry.url} ${res.status}`);
  }
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_WEBSOCKET_TEMPLATE = '''
import ws from 'k6/ws';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: __THRESHOLDS__,
  noConnectionReuse: true,
};

const WS_URL = `__TARGET_URL____WS_ENDPOINT__`;
const WS_MESSAGE = `__WS_MESSAGE__`;

export default function () {
  const res = ws.connect(WS_URL, null, function (socket) {
    socket.on('open', function () {
      socket.send(WS_MESSAGE);
    });
    socket.on('error', function (e) {
      console.log(`WS ERROR: ${e.error()}`);
    });
    socket.setTimeout(function () {
      socket.close();
    }, 5000);
  });
  check(res, { 'ws connected': (r) => r && r.status === 101 });
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_GRPC_TEMPLATE = '''
import grpc from 'k6/net/grpc';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: __THRESHOLDS__,
  noConnectionReuse: true,
};

const client = new grpc.Client();
client.load([], '__GRPC_PROTO__');
const GRPC_ADDRESS = `__GRPC_ADDRESS__`;
const GRPC_METHOD = '__GRPC_SERVICE__/__GRPC_METHOD__';

export default function () {
  client.connect(GRPC_ADDRESS, {});
  const response = client.invoke(GRPC_METHOD, {});
  check(response, {
    'grpc status ok': (r) => r && r.status === grpc.StatusOK,
  });
  if (!response || response.status !== grpc.StatusOK) {
    console.log(`GRPC FAIL: ${JSON.stringify(response)}`);
  }
  client.close();
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_BROWSER_TEMPLATE = '''
import { browser } from 'k6/browser';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '__STAGE_RAMP_UP__', target: __USERS__ },
    { duration: '__STAGE_SUSTAIN__', target: __USERS__ },
    { duration: '__STAGE_RAMP_DOWN__', target: 0 },
  ],
  thresholds: __THRESHOLDS__,
};

const BASE_URL = `__TARGET_URL__`;
const BROWSER_PATH = `__BROWSER_PATH__`;

export default async function () {
  const page = await browser.newPage();
  try {
    const url = `${BASE_URL}${BROWSER_PATH}`;
    const res = await page.goto(url, { waitUntil: 'networkidle' });
    check(res, {
      'page loaded': (r) => r.status === 200,
    });
    const title = await page.title();
    console.log(`Page title: ${title}`);
  } catch (e) {
    console.log(`BROWSER ERROR: ${e.message}`);
  } finally {
    await page.close();
  }
  sleep(Math.random() * 2 + 0.5);
}
'''

K6_STRESS_TEMPLATE = '''
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: __STRESS_STAGES__,
  thresholds: __THRESHOLDS__,
  noConnectionReuse: true,
};

const BASE_URL = `__TARGET_URL__`;
const STRESS_ENDPOINT = `__STRESS_ENDPOINT__`;
const STRESS_METHOD = `__STRESS_METHOD__`;
const STRESS_BODY = `__STRESS_BODY__`;

export default function () {
  const url = `${BASE_URL}${STRESS_ENDPOINT}`;
  const params = {
    headers: __HEADERS__,
    tags: { test_type: 'stress' },
  };

  let res;
  switch (STRESS_METHOD) {
    case 'GET': res = http.get(url, params); break;
    case 'POST': res = http.post(url, STRESS_BODY || '', params); break;
    case 'PUT': res = http.put(url, STRESS_BODY || '', params); break;
    default: res = http.get(url, params);
  }

  check(res, {
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  if (res.status >= 400) {
    console.log(`STRESS FAIL: ${res.status} ${res.body}`);
  }
  sleep(Math.random() * 1 + 0.3);
}
'''

TEMPLATES = {
    "login": K6_LOGIN_TEMPLATE,
    "upload": K6_UPLOAD_TEMPLATE,
    "both": K6_COMBINED_TEMPLATE,
    "generic": K6_GENERIC_TEMPLATE,
    "har": K6_HAR_TEMPLATE,
    "websocket": K6_WEBSOCKET_TEMPLATE,
    "grpc": K6_GRPC_TEMPLATE,
    "browser": K6_BROWSER_TEMPLATE,
    "stress": K6_STRESS_TEMPLATE,
}


def generate_stress_stages(initial_users, max_users, step_duration):
    stages = []
    current = initial_users
    while current <= max_users:
        stages.append({"duration": step_duration, "target": current})
        if current >= max_users:
            break
        current = min(current * 2, max_users)
    stages.append({"duration": "30s", "target": 0})
    return stages


def normalize_url(raw):
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        return f"https://{raw}"
    return raw


def validate_params(data, test_type):
    errors = []
    target = data.get("target_url", "").strip()
    if not target:
        errors.append("target_url: es requerido")

    raw_users = data.get("users", 10)
    try:
        users = int(raw_users)
        if users < 1:
            errors.append("users: debe ser >= 1")
        elif users > 1000000:
            errors.append("users: debe ser <= 1000000")
    except (ValueError, TypeError):
        errors.append("users: debe ser un número entero")

    if data.get("distributed", False):
        try:
            instances = int(data.get("instances", 2))
            if instances < 2:
                errors.append("instances: debe ser >= 2 en modo distribuido")
            elif instances > 64:
                errors.append("instances: debe ser <= 64")
        except (ValueError, TypeError):
            errors.append("instances: debe ser un número entero")

    if test_type in ("upload", "both", "generic"):
        try:
            file_size = int(data.get("file_size", 1024))
            if file_size < 1:
                errors.append("file_size: debe ser >= 1")
            elif file_size > 10485760:
                errors.append("file_size: debe ser <= 10485760 (10MB)")
        except (ValueError, TypeError):
            errors.append("file_size: debe ser un número entero")

    if test_type == "generic":
        requests = data.get("requests", [])
        if not requests or not isinstance(requests, list):
            errors.append("requests: debe ser una lista no vacía")
        else:
            for i, req in enumerate(requests):
                if not req.get("path"):
                    errors.append(f"requests[{i}].path: es requerido")
                method = req.get("method", "GET").upper()
                if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    errors.append(f"requests[{i}].method: método inválido")

    if test_type == "websocket":
        ws_endpoint = data.get("ws_endpoint", "")
        if not ws_endpoint:
            errors.append("ws_endpoint: es requerido para pruebas WebSocket")

    if test_type == "grpc":
        if not data.get("grpc_address"):
            errors.append("grpc_address: es requerido para pruebas gRPC")
        if not data.get("grpc_proto"):
            errors.append("grpc_proto: es requerido (ruta al .proto)")
        if not data.get("grpc_service"):
            errors.append("grpc_service: es requerido")
        if not data.get("grpc_method"):
            errors.append("grpc_method: es requerido")

    if test_type == "browser":
        if not data.get("browser_path"):
            errors.append("browser_path: es requerido")

    if test_type == "stress":
        try:
            initial = int(data.get("stress_initial_users", 5))
            max_u = int(data.get("stress_max_users", 1000))
            if initial < 1:
                errors.append("stress_initial_users: debe ser >= 1")
            if max_u < initial:
                errors.append("stress_max_users: debe ser >= stress_initial_users")
        except (ValueError, TypeError):
            errors.append("stress_initial_users/stress_max_users: deben ser enteros")

    return errors


def parse_headers(headers_raw):
    if not headers_raw:
        return {}
    if isinstance(headers_raw, dict):
        return {k: str(v) for k, v in headers_raw.items()}
    if isinstance(headers_raw, str):
        headers_raw = headers_raw.strip()
        if not headers_raw:
            return {}
        try:
            parsed = json.loads(headers_raw)
            if isinstance(parsed, dict):
                return {k: str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass
        result = {}
        for line in headers_raw.split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result
    return {}


def build_thresholds_json(data, test_type):
    base = {
        "http_req_duration": [f"p(95)<{data.get('threshold_p95', 5000)}"],
        "http_req_failed": [f"rate<{data.get('threshold_fail_rate', 0.1)}"],
    }
    custom = data.get("custom_thresholds", {})
    if isinstance(custom, dict):
        base.update(custom)
    return json.dumps(base, ensure_ascii=False)


def generate_script(test_type, params):
    template = TEMPLATES.get(test_type, K6_LOGIN_TEMPLATE)
    replacements = {
        "__TARGET_URL__": params["target_url"],
        "__USERS__": str(params["users"]),
        "__STAGE_RAMP_UP__": params.get("stage_ramp_up", "30s"),
        "__STAGE_SUSTAIN__": params.get("stage_sustain", "1m"),
        "__STAGE_RAMP_DOWN__": params.get("stage_ramp_down", "30s"),
        "__THRESHOLDS__": params.get("_thresholds_json", '{"http_req_duration":["p(95)<5000"],"http_req_failed":["rate<0.1"]}'),
    }

    if test_type in ("login", "both"):
        login_headers = parse_headers(params.get("login_headers", {}))
        replacements["__LOGIN_ENDPOINT__"] = params.get("login_endpoint", "/login")
        replacements["__USERNAME__"] = params.get("username", "test")
        replacements["__PASSWORD__"] = params.get("password", "test")
        replacements["__USERNAME_FIELD__"] = params.get("username_field", "username")
        replacements["__PASSWORD_FIELD__"] = params.get("password_field", "password")
        replacements["__LOGIN_HEADERS__"] = json.dumps(login_headers, ensure_ascii=False)
        replacements["__HEADERS__"] = json.dumps(login_headers, ensure_ascii=False)

    if test_type in ("upload", "both"):
        upload_headers = parse_headers(params.get("upload_headers", {}))
        replacements["__UPLOAD_ENDPOINT__"] = params.get("upload_endpoint", "/upload")
        replacements["__FILE_SIZE__"] = str(params.get("file_size", 1024))
        replacements["__FILE_FIELD__"] = params.get("file_field", "file")
        replacements["__UPLOAD_METHOD__"] = params.get("upload_method", "POST")
        replacements["__UPLOAD_HEADERS__"] = json.dumps(upload_headers, ensure_ascii=False)
        replacements["__HEADERS__"] = json.dumps(upload_headers, ensure_ascii=False)

    if test_type == "generic":
        requests = params.get("requests", [])
        for req in requests:
            req.setdefault("headers", {})
            req.setdefault("body", "")
        replacements["__REQUESTS__"] = json.dumps(requests, ensure_ascii=False)

    if test_type == "har":
        entries = params.get("har_entries", [])
        replacements["__HAR_ENTRIES__"] = json.dumps(entries, ensure_ascii=False)

    if test_type == "websocket":
        replacements["__WS_ENDPOINT__"] = params.get("ws_endpoint", "/ws")
        replacements["__WS_MESSAGE__"] = params.get("ws_message", "ping")

    if test_type == "grpc":
        replacements["__GRPC_ADDRESS__"] = params.get("grpc_address", "")
        replacements["__GRPC_PROTO__"] = params.get("grpc_proto", "")
        replacements["__GRPC_SERVICE__"] = params.get("grpc_service", "")
        replacements["__GRPC_METHOD__"] = params.get("grpc_method", "")

    if test_type == "browser":
        replacements["__BROWSER_PATH__"] = params.get("browser_path", "/")

    if test_type == "stress":
        initial = int(params.get("stress_initial_users", 5))
        max_users = int(params.get("stress_max_users", 1000))
        step_dur = params.get("stress_step_duration", "30s")
        stages = generate_stress_stages(initial, max_users, step_dur)
        replacements["__STRESS_STAGES__"] = json.dumps(stages, ensure_ascii=False)
        replacements["__STRESS_ENDPOINT__"] = params.get("stress_endpoint", "/")
        replacements["__STRESS_METHOD__"] = params.get("stress_method", "GET")
        replacements["__STRESS_BODY__"] = params.get("stress_body", "")
        stress_headers = parse_headers(params.get("stress_headers", {}))
        replacements["__HEADERS__"] = json.dumps(stress_headers, ensure_ascii=False)

    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result


class NdjsonReader:
    def __init__(self, path):
        self.path = path
        self.fp = None
        self.last_pos = 0

    def open(self):
        try:
            self.fp = open(self.path, "r")
            self.last_pos = 0
        except Exception:
            self.fp = None

    def read_new_lines(self):
        if not self.fp:
            return []
        try:
            self.fp.seek(self.last_pos)
            lines = self.fp.readlines()
            self.last_pos = self.fp.tell()
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

    def close(self):
        if self.fp:
            self.fp.close()


def aggregate_ndjson(lines, prev=None):
    if prev is None:
        prev = {}
    for line in lines:
        metric = line.get("metric", "")
        data = line.get("data", {})
        value = data.get("value", 0)
        mtype = line.get("type", "")

        if metric == "http_reqs":
            m = prev.setdefault("http_reqs", {"count": 0})
            m["count"] = m.get("count", 0) + 1

        elif metric == "http_req_duration" and mtype == "Point":
            m = prev.setdefault("http_req_duration", {"count": 0, "sum": 0, "min": float("inf"), "max": float("-inf")})
            m["count"] = m.get("count", 0) + 1
            m["sum"] = m.get("sum", 0) + value
            m["min"] = min(m.get("min", float("inf")), value)
            m["max"] = max(m.get("max", float("-inf")), value)
            count = m["count"]
            m["avg"] = m["sum"] / count if count else 0

        elif metric == "http_req_failed" and mtype == "Point":
            m = prev.setdefault("http_req_failed", {"count": 0, "failures": 0})
            m["count"] = m.get("count", 0) + 1
            if value > 0:
                m["failures"] = m.get("failures", 0) + 1
            total = m["count"]
            m["rate"] = m["failures"] / total if total else 0

    return prev


def compute_intermediate_snapshot(aggregated, elapsed):
    snapshot = {
        "time": datetime.now(timezone.utc).isoformat(),
        "elapsed": elapsed,
    }
    reqs = aggregated.get("http_reqs", {})
    duration = aggregated.get("http_req_duration", {})
    failed = aggregated.get("http_req_failed", {})

    snapshot["http_reqs"] = reqs.get("count", 0)
    snapshot["http_req_rate"] = reqs.get("count", 0) / elapsed if elapsed > 0 else 0
    snapshot["avg_duration"] = duration.get("avg", 0)
    snapshot["min_duration"] = duration.get("min", 0) if duration.get("min", float("inf")) != float("inf") else 0
    snapshot["max_duration"] = duration.get("max", 0) if duration.get("max", float("-inf")) != float("-inf") else 0
    snapshot["fail_rate"] = failed.get("rate", 0)
    return snapshot


def aggregate_summaries(summaries):
    if not summaries:
        return {}
    if len(summaries) == 1:
        return summaries[0]

    merged = {"metrics": {}, "root_group": {"groups": {}, "checks": {}}}
    for s in summaries:
        m = s.get("metrics", {})
        for key, sm in m.items():
            am = merged["metrics"].setdefault(key, {})
            cnt = sm.get("count", 0)
            if "count" in sm:
                am["count"] = am.get("count", 0) + cnt
            if "rate" in sm and cnt:
                rate_sum = am.get("_rate_sum", 0.0) + sm["rate"] * cnt
                count_sum = am.get("_count_sum", 0) + cnt
                am["_rate_sum"] = rate_sum
                am["_count_sum"] = count_sum
                am["rate"] = rate_sum / count_sum
            if "avg" in sm:
                if cnt:
                    avg_sum = am.get("_avg_sum", 0.0) + sm["avg"] * cnt
                    count_for_avg = am.get("_avg_count", 0) + cnt
                    am["_avg_sum"] = avg_sum
                    am["_avg_count"] = count_for_avg
                    am["avg"] = avg_sum / count_for_avg
                else:
                    avg_sum = am.get("_avg_sum", 0.0) + sm["avg"]
                    avg_n = am.get("_avg_n", 0) + 1
                    am["_avg_sum"] = avg_sum
                    am["_avg_n"] = avg_n
                    am["avg"] = avg_sum / avg_n
            if "min" in sm:
                am["min"] = min(am.get("min", sm["min"]), sm["min"])
            if "max" in sm:
                am["max"] = max(am.get("max", sm["max"]), sm["max"])
            if "med" in sm:
                if cnt:
                    med_sum = am.get("_med_sum", 0.0) + sm["med"] * cnt
                    count_for_med = am.get("_med_count", 0) + cnt
                    am["_med_sum"] = med_sum
                    am["_med_count"] = count_for_med
                    am["med"] = med_sum / count_for_med
                else:
                    med_sum = am.get("_med_sum", 0.0) + sm["med"]
                    med_n = am.get("_med_n", 0) + 1
                    am["_med_sum"] = med_sum
                    am["_med_n"] = med_n
                    am["med"] = med_sum / med_n
            if "p(95)" in sm:
                p95 = am.get("_p95_max", 0.0)
                am["_p95_max"] = max(p95, sm["p(95)"])
                am["p(95)"] = am["_p95_max"]
            if "p(99)" in sm:
                p99 = am.get("_p99_max", 0.0)
                am["_p99_max"] = max(p99, sm["p(99)"])
                am["p(99)"] = am["_p99_max"]
            if "type" in sm:
                am["type"] = sm["type"]
            if "contains" in sm:
                am["contains"] = sm["contains"]
            if "values" in sm:
                vals = am.setdefault("values", {})
                for vk, vv in sm["values"].items():
                    if isinstance(vv, (int, float)):
                        vals[vk] = vals.get(vk, 0) + vv
                    else:
                        vals[vk] = vv

    cleanup_keys = ["_rate_sum", "_count_sum", "_avg_sum", "_avg_count", "_avg_n", "_med_sum", "_med_count", "_med_n", "_p95_max", "_p99_max"]
    for key in list(merged.get("metrics", {}).keys()):
        m = merged["metrics"][key]
        for ck in cleanup_keys:
            m.pop(ck, None)

    return merged


def run_single_instance(test_id, script_path, results_file, ndjson_path=None, instance_index=0, total_instances=1):
    cmd = [K6_EXE, "run", "--summary-export", str(results_file), "--quiet", str(script_path)]
    if ndjson_path:
        cmd.extend(["--out", f"json={ndjson_path}"])

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        error_msg = f"[instancia {instance_index + 1}/{total_instances}] ERROR: k6 no encontrado en {K6_EXE}"
        if test_id in active_tests:
            active_tests[test_id]["output"].append(error_msg)
            active_tests[test_id]["error"] = error_msg
        return -1, None
    except Exception as e:
        error_msg = f"[instancia {instance_index + 1}/{total_instances}] ERROR: {str(e)}"
        if test_id in active_tests:
            active_tests[test_id]["output"].append(error_msg)
            active_tests[test_id]["error"] = error_msg
        return -1, None

    cancel_event = cancel_events.get(test_id)
    for line in proc.stdout:
        if test_id in active_tests:
            combined = active_tests[test_id].get("output", [])
            combined.append(f"[instancia {instance_index + 1}/{total_instances}] {line.rstrip()}")
            if len(combined) > 500:
                combined = combined[-300:]
            active_tests[test_id]["output"] = combined
        if cancel_event and cancel_event.is_set():
            proc.kill()
            break

    proc.wait()
    if cancel_event and cancel_event.is_set():
        if test_id in active_tests:
            active_tests[test_id]["output"].append(f"[instancia {instance_index + 1}/{total_instances}] ⛔ Test cancelado")
        return -9, None

    return proc.returncode, results_file if results_file.exists() else None


def save_to_history(test_id, test_data):
    history_file = HISTORY_DIR / f"{test_id}.json"
    try:
        history_file.write_text(json.dumps(test_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_from_history():
    loaded = 0
    for f in sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            tid = f.stem
            data["done"] = True
            data["_from_history"] = True
            active_tests[tid] = data
            loaded += 1
        except Exception:
            pass
    return loaded


def cleanup_old_files(max_age_days=None):
    if max_age_days is None:
        max_age_days = MAX_SCRIPT_AGE_DAYS
    now = time_module.time()
    cutoff = now - (max_age_days * 86400)
    cleaned = 0
    for d in [SCRIPTS_DIR, RESULTS_DIR]:
        for f in d.glob("*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleaned += 1
            except Exception:
                pass
    return cleaned


def get_k6_version():
    try:
        result = subprocess.run([K6_EXE, "version"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def parse_har(har_data):
    entries = []
    try:
        log = har_data.get("log", {})
        for entry in log.get("entries", []):
            req = entry.get("request", {})
            url = req.get("url", "")
            method = req.get("method", "GET").upper()
            headers = {}
            for h in req.get("headers", []):
                name = h.get("name", "")
                value = h.get("value", "")
                if name.lower() not in ("cookie", "content-length", "host"):
                    headers[name] = value
            body = None
            post_data = req.get("postData", {})
            if post_data:
                body = post_data.get("text", "")
            entries.append({"url": url, "method": method, "headers": headers, "body": body or ""})
    except Exception:
        pass
    return entries


def load_schedules():
    global schedules
    try:
        if SCHEDULES_FILE.exists():
            schedules = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        else:
            schedules = []
    except Exception:
        schedules = []


def save_schedules():
    try:
        SCHEDULES_FILE.write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_webhooks():
    global webhooks
    try:
        if WEBHOOKS_FILE.exists():
            webhooks = json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
        else:
            webhooks = []
    except Exception:
        webhooks = []


def save_webhooks():
    try:
        WEBHOOKS_FILE.write_text(json.dumps(webhooks, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def fire_webhooks(event_type, test_id, test_data):
    for wh in webhooks:
        if not wh.get("enabled", True):
            continue
        if event_type not in wh.get("events", ["completed"]):
            continue
        url = wh.get("url", "")
        if not url:
            continue
        try:
            payload = json.dumps({
                "event": event_type,
                "test_id": test_id,
                "data": {
                    "type": test_data.get("type"),
                    "users": test_data.get("users"),
                    "target": test_data.get("target"),
                    "returncode": test_data.get("returncode"),
                    "error": test_data.get("error"),
                    "timestamp": test_data.get("timestamp"),
                },
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass


def scheduler_loop():
    global scheduler_running
    while scheduler_running:
        try:
            now = datetime.now(timezone.utc)
            load_schedules()
            for sched in schedules:
                if not sched.get("enabled", True):
                    continue
                next_run_str = sched.get("next_run")
                if not next_run_str:
                    continue
                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except Exception:
                    continue
                if now >= next_run:
                    sched_id = sched.get("id")
                    config = sched.get("config", {})
                    if config:
                        run_scheduled_test(sched_id, config)
                    interval_min = sched.get("interval_minutes", 60)
                    sched["next_run"] = (now + timedelta(minutes=interval_min)).isoformat()
                    sched["last_run"] = now.isoformat()
                    save_schedules()
        except Exception:
            pass
        time_module.sleep(30)


def run_scheduled_test(sched_id, config):
    data = config
    test_id = uuid.uuid4().hex[:12]
    target_url = normalize_url(data.get("target_url", ""))
    test_type = data.get("test_type", "login")
    users = int(data.get("users", 10))
    distributed = data.get("distributed", False)
    instances = max(1, min(int(data.get("instances", 2)), 64))
    timestamp = datetime.now(timezone.utc).isoformat()

    params = build_params(data, test_type, target_url, users)

    active_tests[test_id] = {
        "done": False, "canceled": False, "output": [], "returncode": None,
        "summary": None, "error": None, "type": test_type, "users": users,
        "target": target_url, "distributed": distributed,
        "instances": instances if distributed else 1,
        "timestamp": timestamp, "params": data, "scheduled": True, "sched_id": sched_id,
    }
    cancel_events[test_id] = threading.Event()

    with active_test_lock:
        global active_test_count
        active_test_count += 1

    def run():
        global active_test_count
        try:
            script_content = generate_script(test_type, params)
            ndjson_path = RESULTS_DIR / f"{test_id}.ndjson"
            script_path = SCRIPTS_DIR / f"{test_id}.js"
            script_path.write_text(script_content, encoding="utf-8")
            results_file = RESULTS_DIR / f"{test_id}.json"
            rc, rf = run_single_instance(test_id, script_path, results_file, str(ndjson_path))
            if test_id in active_tests:
                active_tests[test_id]["done"] = True
                active_tests[test_id]["returncode"] = rc
                if rf:
                    try:
                        active_tests[test_id]["summary"] = json.loads(rf.read_text(encoding="utf-8"))
                    except Exception as e:
                        active_tests[test_id]["error"] = f"Error leyendo resultados: {e}"
                save_to_history(test_id, active_tests[test_id])
                fire_webhooks("completed" if rc == 0 else "failed", test_id, active_tests[test_id])
        finally:
            with active_test_lock:
                active_test_count -= 1

    t = threading.Thread(target=run, daemon=True)
    t.start()


def build_params(data, test_type, target_url, users):
    login_headers = parse_headers(data.get("login_headers", {}))
    upload_headers = parse_headers(data.get("upload_headers", {}))
    thresholds_json = build_thresholds_json(data, test_type)

    params = {
        "target_url": target_url,
        "users": users,
        "login_endpoint": data.get("login_endpoint", "/login"),
        "upload_endpoint": data.get("upload_endpoint", "/upload"),
        "username": data.get("username", "test"),
        "password": data.get("password", "test"),
        "username_field": data.get("username_field", "username"),
        "password_field": data.get("password_field", "password"),
        "file_field": data.get("file_field", "file"),
        "file_size": int(data.get("file_size", 1024)),
        "stage_ramp_up": data.get("stage_ramp_up", "30s"),
        "stage_sustain": data.get("stage_sustain", "1m"),
        "stage_ramp_down": data.get("stage_ramp_down", "30s"),
        "login_headers": login_headers,
        "upload_headers": upload_headers,
        "_thresholds_json": thresholds_json,
    }

    extra = [
        "ws_endpoint", "ws_message",
        "grpc_address", "grpc_proto", "grpc_service", "grpc_method",
        "browser_path",
        "stress_initial_users", "stress_max_users", "stress_step_duration",
        "stress_endpoint", "stress_method", "stress_body", "stress_headers",
        "requests", "har_entries", "upload_method", "custom_thresholds",
    ]
    for key in extra:
        val = data.get(key)
        if val is not None:
            params[key] = val

    return params


app = Flask(__name__)

if HAS_SWAGGER:
    swagger = Swagger(app, template={
        "info": {
            "title": "Load Tester API",
            "description": "API para pruebas de carga con k6",
            "version": "2.0.0",
        },
        "basePath": "/api",
    })

DEFAULT_SWAGGER = {
    "produces": ["application/json"],
    "consumes": ["application/json"],
}


@app.route("/")
def index():
    """Página principal"""
    return render_template("index.html")


@app.route("/api/docs")
def api_docs():
    """Documentación OpenAPI de la API"""
    docs = {
        "openapi": "3.0.0",
        "info": {
            "title": "Load Tester API",
            "version": "2.0.0",
            "description": "API para ejecutar y gestionar pruebas de carga con k6",
        },
        "paths": {
            "/api/status": {"get": {"summary": "Estado del servidor k6", "responses": {"200": {"description": "OK"}}}},
            "/api/run": {"post": {"summary": "Ejecutar una prueba de carga", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RunRequest"}}}}, "responses": {"200": {"description": "Test iniciado"}}}},
            "/api/cancel/{test_id}": {"post": {"summary": "Cancelar una prueba en curso", "parameters": [{"name": "test_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Test cancelado"}}}},
            "/api/results/{test_id}": {"get": {"summary": "Obtener resultados de una prueba", "parameters": [{"name": "test_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Resultados"}}}},
            "/api/stream/{test_id}": {"get": {"summary": "Streaming SSE de una prueba", "parameters": [{"name": "test_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Event stream"}}}},
            "/api/history": {"get": {"summary": "Historial de pruebas", "responses": {"200": {"description": "Lista de pruebas"}}}},
            "/api/export/{test_id}/{fmt}": {"get": {"summary": "Exportar resultados (json/csv)", "parameters": [{"name": "test_id", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "fmt", "in": "path", "required": True, "schema": {"type": "string", "enum": ["json", "csv"]}}], "responses": {"200": {"description": "Archivo exportado"}}}},
            "/api/cleanup": {"post": {"summary": "Limpiar scripts y resultados viejos", "responses": {"200": {"description": "Limpieza completada"}}}},
            "/api/har/parse": {"post": {"summary": "Parsear archivo HAR", "responses": {"200": {"description": "Entradas parseadas"}}}},
            "/api/schedules": {"get": {"summary": "Listar tareas programadas", "responses": {"200": {"description": "Lista de schedules"}}}, "post": {"summary": "Crear tarea programada", "responses": {"200": {"description": "Schedule creado"}}}},
            "/api/schedules/{sched_id}": {"delete": {"summary": "Eliminar tarea programada", "parameters": [{"name": "sched_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Eliminado"}}}},
            "/api/webhooks": {"get": {"summary": "Listar webhooks", "responses": {"200": {"description": "Lista de webhooks"}}}, "post": {"summary": "Configurar webhook", "responses": {"200": {"description": "Webhook configurado"}}}},
            "/api/webhooks/{wh_id}": {"delete": {"summary": "Eliminar webhook", "parameters": [{"name": "wh_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Eliminado"}}}},
            "/api/comparison": {"post": {"summary": "Ejecutar comparativa lado a lado", "responses": {"200": {"description": "Comparativa iniciada"}}}},
            "/api/comparison/{comp_id}": {"get": {"summary": "Resultados de comparativa", "parameters": [{"name": "comp_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Resultados"}}}},
            "/api/responses/{test_id}": {"get": {"summary": "Log de respuestas fallidas", "parameters": [{"name": "test_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Lista de fallos"}}}},
        },
        "components": {
            "schemas": {
                "RunRequest": {
                    "type": "object",
                    "properties": {
                        "target_url": {"type": "string", "example": "https://example.com"},
                        "test_type": {"type": "string", "enum": ["login", "upload", "both", "generic", "har", "websocket", "grpc", "browser", "stress"]},
                        "users": {"type": "integer", "example": 100},
                    },
                    "required": ["target_url", "test_type"],
                }
            }
        },
    }
    return jsonify(docs)


@app.route("/api/status")
def api_status():
    k6_ok = os.path.exists(K6_EXE)
    return jsonify({
        "k6": k6_ok,
        "k6_path": K6_EXE,
        "version": get_k6_version() if k6_ok else None,
        "active_tests": len([t for t in active_tests.values() if not t.get("done")]),
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    global active_test_count
    with active_test_lock:
        if active_test_count >= MAX_CONCURRENT_TESTS:
            return jsonify({"error": f"Límite de {MAX_CONCURRENT_TESTS} pruebas concurrentes alcanzado"}), 429

    data = request.get_json()
    if not data:
        return jsonify({"error": "Cuerpo de solicitud inválido"}), 400

    test_type = data.get("test_type", "login")
    valid_types = ("login", "upload", "both", "generic", "har", "websocket", "grpc", "browser", "stress")
    if test_type not in valid_types:
        return jsonify({"error": f"Tipo de prueba inválido: {test_type}"}), 400

    errors = validate_params(data, test_type)
    if errors:
        return jsonify({"error": "Errores de validación", "details": errors}), 400

    target_url = normalize_url(data.get("target_url", ""))
    users = int(data.get("users", 10))
    distributed = data.get("distributed", False)
    instances = max(1, min(int(data.get("instances", 2)), 64))
    timestamp = datetime.now(timezone.utc).isoformat()
    test_id = uuid.uuid4().hex[:12]

    params = build_params(data, test_type, target_url, users)

    active_tests[test_id] = {
        "done": False, "canceled": False, "output": [], "intermediate": None,
        "returncode": None, "summary": None, "error": None, "type": test_type,
        "users": users, "target": target_url, "distributed": distributed,
        "instances": instances if distributed else 1,
        "timestamp": timestamp, "params": data, "snapshots": [],
    }
    cancel_events[test_id] = threading.Event()

    with active_test_lock:
        active_test_count += 1

    def run_intermediate_tracker(tid, ndjson_path):
        reader = NdjsonReader(ndjson_path)
        reader.open()
        start = time_module.time()
        aggregated = {}
        while tid in active_tests and not active_tests[tid].get("done"):
            time_module.sleep(2)
            lines = reader.read_new_lines()
            if lines:
                aggregated = aggregate_ndjson(lines, aggregated)
                elapsed = time_module.time() - start
                snapshot = compute_intermediate_snapshot(aggregated, elapsed)
                if tid in active_tests:
                    snapshots = active_tests[tid].setdefault("snapshots", [])
                    snapshots.append(snapshot)
                    if len(snapshots) > 200:
                        snapshots = snapshots[-200:]
                    active_tests[tid]["snapshots"] = snapshots
                    active_tests[tid]["intermediate"] = snapshot
        reader.close()

    def run_normal():
        global active_test_count
        try:
            script_content = generate_script(test_type, params)
            ndjson_path = RESULTS_DIR / f"{test_id}.ndjson"
            script_path = SCRIPTS_DIR / f"{test_id}.js"
            script_path.write_text(script_content, encoding="utf-8")
            results_file = RESULTS_DIR / f"{test_id}.json"

            tracker_thread = threading.Thread(target=run_intermediate_tracker, args=(test_id, ndjson_path), daemon=True)
            tracker_thread.start()

            rc, rf = run_single_instance(test_id, script_path, results_file, str(ndjson_path))
            time_module.sleep(0.5)

            if test_id in active_tests:
                active_tests[test_id]["done"] = True
                active_tests[test_id]["returncode"] = rc
                if rf:
                    try:
                        active_tests[test_id]["summary"] = json.loads(rf.read_text(encoding="utf-8"))
                    except Exception as e:
                        active_tests[test_id]["error"] = f"Error leyendo resultados: {e}"
                save_to_history(test_id, active_tests[test_id])
                fire_webhooks("completed" if rc == 0 else "failed", test_id, active_tests[test_id])
        finally:
            with active_test_lock:
                active_test_count -= 1

    def run_distributed():
        global active_test_count
        try:
            users_per_instance = max(1, users // instances)
            threads = []
            results_files = []
            script_paths = []
            ndjson_paths = []

            for i in range(instances):
                instance_params = dict(params)
                instance_params["users"] = users_per_instance
                if i == instances - 1:
                    sobra = users - (users_per_instance * instances)
                    instance_params["users"] = users_per_instance + sobra

                script_content = generate_script(test_type, instance_params)
                sp = SCRIPTS_DIR / f"{test_id}_inst{i}.js"
                sp.write_text(script_content, encoding="utf-8")
                script_paths.append(sp)

                rf = RESULTS_DIR / f"{test_id}_inst{i}.json"
                results_files.append(rf)
                ndjson_paths.append(RESULTS_DIR / f"{test_id}_inst{i}.ndjson")

            summaries = []
            all_ok = True

            def run_instance(i):
                nonlocal all_ok
                rc, rf = run_single_instance(test_id, script_paths[i], results_files[i], str(ndjson_paths[i]), i, instances)
                if rc != 0:
                    all_ok = False
                if rf:
                    try:
                        summaries.append(json.loads(rf.read_text(encoding="utf-8")))
                    except Exception:
                        pass

            for i in range(instances):
                t = threading.Thread(target=run_instance, args=(i,), daemon=True)
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            if test_id in active_tests:
                aggregated = aggregate_summaries(summaries)
                active_tests[test_id]["done"] = True
                active_tests[test_id]["returncode"] = 0 if all_ok and not cancel_events.get(test_id, threading.Event()).is_set() else 1
                active_tests[test_id]["summary"] = aggregated
                save_to_history(test_id, active_tests[test_id])
                fire_webhooks("completed" if all_ok else "failed", test_id, active_tests[test_id])
        finally:
            with active_test_lock:
                active_test_count -= 1

    if distributed:
        t = threading.Thread(target=run_distributed, daemon=True)
    else:
        t = threading.Thread(target=run_normal, daemon=True)
    t.start()

    mode_str = f"{instances} instancias en paralelo" if distributed else "1 instancia"
    return jsonify({
        "test_id": test_id,
        "message": f"Test {test_id} iniciado - {users} usuarios ({mode_str}) en {target_url}",
    })


@app.route("/api/cancel/<test_id>", methods=["POST"])
def api_cancel(test_id):
    test = active_tests.get(test_id)
    if not test:
        return jsonify({"error": "Test no encontrado"}), 404
    if test.get("done"):
        return jsonify({"error": "El test ya ha terminado"}), 400

    cancel_event = cancel_events.get(test_id)
    if cancel_event:
        cancel_event.set()
    test["canceled"] = True
    test["done"] = True
    test["returncode"] = -9
    test["output"].append("⛛ Test cancelado por el usuario")
    save_to_history(test_id, test)
    fire_webhooks("canceled", test_id, test)

    return jsonify({"message": f"Test {test_id} cancelado"})


@app.route("/api/results/<test_id>")
def api_results(test_id):
    test = active_tests.get(test_id)
    if not test:
        return jsonify({"error": "Test no encontrado"}), 404

    return jsonify({
        "test_id": test_id,
        "done": test.get("done"),
        "canceled": test.get("canceled", False),
        "returncode": test.get("returncode"),
        "output": test.get("output", []),
        "summary": test.get("summary"),
        "intermediate": test.get("intermediate"),
        "snapshots": test.get("snapshots", []),
        "error": test.get("error"),
        "type": test.get("type"),
        "users": test.get("users"),
        "target": test.get("target"),
        "distributed": test.get("distributed", False),
        "instances": test.get("instances", 1),
        "timestamp": test.get("timestamp"),
    })


@app.route("/api/stream/<test_id>")
def api_stream(test_id):
    def generate():
        while True:
            test = active_tests.get(test_id)
            if not test:
                yield f"data: {json.dumps({'error': 'test not found'})}\n\n"
                break
            yield f"data: {json.dumps({'done': test.get('done'), 'output': test.get('output', [])[-20:], 'summary': test.get('summary'), 'intermediate': test.get('intermediate'), 'snapshots': test.get('snapshots', [])[-50:], 'returncode': test.get('returncode'), 'canceled': test.get('canceled', False), 'error': test.get('error')})}\n\n"
            if test.get("done"):
                break
            time_module.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/history")
def api_history():
    entries = []
    seen = set()
    for tid, test in sorted(active_tests.items(), key=lambda x: x[0], reverse=True)[:200]:
        if tid in seen:
            continue
        seen.add(tid)
        entries.append({
            "test_id": tid,
            "type": test.get("type"),
            "users": test.get("users"),
            "target": test.get("target"),
            "done": test.get("done"),
            "returncode": test.get("returncode"),
            "distributed": test.get("distributed", False),
            "instances": test.get("instances", 1),
            "timestamp": test.get("timestamp"),
            "canceled": test.get("canceled", False),
            "error": test.get("error"),
            "scheduled": test.get("scheduled", False),
        })
    return jsonify(entries)


@app.route("/api/export/<test_id>/<fmt>")
def api_export(test_id, fmt):
    test = active_tests.get(test_id)
    if not test:
        return jsonify({"error": "Test no encontrado"}), 404
    summary = test.get("summary")
    if not summary:
        return jsonify({"error": "No hay resultados para exportar"}), 400

    if fmt == "json":
        resp = jsonify(summary)
        resp.headers["Content-Disposition"] = f"attachment; filename={test_id}.json"
        return resp

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Metrica", "Tipo", "Contador", "Tasa", "Min", "Media", "Mediana", "Max", "p(90)", "p(95)", "p(99)"])
        metrics = summary.get("metrics", {})
        for key, m in metrics.items():
            writer.writerow([
                key, m.get("type", ""), m.get("count", ""), m.get("rate", ""),
                m.get("min", ""), m.get("avg", ""), m.get("med", ""), m.get("max", ""),
                m.get("p(90)", ""), m.get("p(95)", ""), m.get("p(99)", ""),
            ])
        result = output.getvalue()
        return Response(
            result, mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={test_id}.csv"},
        )

    return jsonify({"error": "Formato inválido. Usa 'json' o 'csv'"}), 400


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    data = request.get_json() or {}
    days = int(data.get("days", MAX_SCRIPT_AGE_DAYS))
    cleaned = cleanup_old_files(days)
    return jsonify({"message": "Limpieza completada", "archivos_eliminados": cleaned, "dias": days})


@app.route("/api/har/parse", methods=["POST"])
def api_har_parse():
    if "file" not in request.files:
        return jsonify({"error": "No se envió archivo HAR"}), 400
    file = request.files["file"]
    if not file.filename.endswith(".har"):
        return jsonify({"error": "El archivo debe tener extensión .har"}), 400
    try:
        har_data = json.loads(file.read().decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"Error al parsear HAR: {e}"}), 400
    entries = parse_har(har_data)
    if not entries:
        return jsonify({"error": "No se encontraron entradas en el archivo HAR"}), 400
    return jsonify({"message": f"Se parsearon {len(entries)} peticiones", "entries": entries[:100], "total": len(entries)})


@app.route("/api/responses/<test_id>")
def api_responses(test_id):
    resp_dir = RESPONSES_DIR / test_id
    if not resp_dir.exists():
        return jsonify({"responses": []})
    all_responses = []
    for f in sorted(resp_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)[:500]:
        try:
            all_responses.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return jsonify({"responses": all_responses})


@app.route("/api/schedules", methods=["GET", "POST"])
def api_schedules():
    if request.method == "GET":
        load_schedules()
        return jsonify(schedules)

    data = request.get_json()
    if not data:
        return jsonify({"error": "Cuerpo inválido"}), 400

    sched_id = uuid.uuid4().hex[:12]
    entry = {
        "id": sched_id,
        "name": data.get("name", f"Schedule {sched_id}"),
        "interval_minutes": int(data.get("interval_minutes", 60)),
        "config": data.get("config", {}),
        "next_run": (datetime.now(timezone.utc) + timedelta(minutes=int(data.get("interval_minutes", 60)))).isoformat(),
        "last_run": None,
        "enabled": data.get("enabled", True),
    }
    load_schedules()
    schedules.append(entry)
    save_schedules()
    return jsonify({"message": "Schedule creado", "id": sched_id, "schedule": entry})


@app.route("/api/schedules/<sched_id>", methods=["DELETE"])
def api_schedule_delete(sched_id):
    load_schedules()
    global schedules
    schedules = [s for s in schedules if s.get("id") != sched_id]
    save_schedules()
    return jsonify({"message": "Schedule eliminado"})


@app.route("/api/webhooks", methods=["GET", "POST"])
def api_webhooks():
    if request.method == "GET":
        load_webhooks()
        return jsonify(webhooks)

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "URL es requerida"}), 400

    wh_id = uuid.uuid4().hex[:12]
    entry = {
        "id": wh_id,
        "url": data["url"],
        "events": data.get("events", ["completed"]),
        "enabled": data.get("enabled", True),
    }
    load_webhooks()
    webhooks.append(entry)
    save_webhooks()
    return jsonify({"message": "Webhook configurado", "id": wh_id})


@app.route("/api/webhooks/<wh_id>", methods=["DELETE"])
def api_webhook_delete(wh_id):
    load_webhooks()
    global webhooks
    webhooks = [w for w in webhooks if w.get("id") != wh_id]
    save_webhooks()
    return jsonify({"message": "Webhook eliminado"})


@app.route("/api/comparison", methods=["POST"])
def api_comparison():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Cuerpo inválido"}), 400

    url_a = data.get("url_a", "").strip()
    url_b = data.get("url_b", "").strip()
    if not url_a or not url_b:
        return jsonify({"error": "url_a y url_b son requeridos"}), 400

    common = {k: v for k, v in data.items() if k not in ("url_a", "url_b")}

    config_a = dict(common)
    config_a["target_url"] = url_a
    config_b = dict(common)
    config_b["target_url"] = url_b

    comp_id = uuid.uuid4().hex[:12]
    comparisons[comp_id] = {"id": comp_id, "test_a": None, "test_b": None, "done": False}

    result_a = api_run_internal(config_a)
    result_b = api_run_internal(config_b)

    comparisons[comp_id]["test_a"] = result_a.get("test_id")
    comparisons[comp_id]["test_b"] = result_b.get("test_id")

    def check_done():
        while True:
            ta = active_tests.get(result_a.get("test_id"), {})
            tb = active_tests.get(result_b.get("test_id"), {})
            if ta.get("done") and tb.get("done"):
                comparisons[comp_id]["done"] = True
                break
            time_module.sleep(1)

    t = threading.Thread(target=check_done, daemon=True)
    t.start()

    return jsonify({
        "comp_id": comp_id,
        "test_a": result_a.get("test_id"),
        "test_b": result_b.get("test_id"),
        "message": f"Comparativa {comp_id}: {url_a} vs {url_b}",
    })


def api_run_internal(config):
    test_type = config.get("test_type", "login")
    target_url = normalize_url(config.get("target_url", ""))
    users = int(config.get("users", 10))
    distributed = config.get("distributed", False)
    instances = max(1, min(int(config.get("instances", 2)), 64))
    timestamp = datetime.now(timezone.utc).isoformat()
    test_id = uuid.uuid4().hex[:12]

    params = build_params(config, test_type, target_url, users)

    active_tests[test_id] = {
        "done": False, "canceled": False, "output": [], "intermediate": None,
        "returncode": None, "summary": None, "error": None, "type": test_type,
        "users": users, "target": target_url, "distributed": distributed,
        "instances": instances if distributed else 1,
        "timestamp": timestamp, "params": config, "snapshots": [],
    }
    cancel_events[test_id] = threading.Event()

    def run_normal():
        try:
            script_content = generate_script(test_type, params)
            ndjson_path = RESULTS_DIR / f"{test_id}.ndjson"
            script_path = SCRIPTS_DIR / f"{test_id}.js"
            script_path.write_text(script_content, encoding="utf-8")
            results_file = RESULTS_DIR / f"{test_id}.json"

            def tracker():
                reader = NdjsonReader(ndjson_path)
                reader.open()
                start = time_module.time()
                aggregated = {}
                while test_id in active_tests and not active_tests[test_id].get("done"):
                    time_module.sleep(2)
                    lines = reader.read_new_lines()
                    if lines:
                        aggregated = aggregate_ndjson(lines, aggregated)
                        elapsed = time_module.time() - start
                        snapshot = compute_intermediate_snapshot(aggregated, elapsed)
                        if test_id in active_tests:
                            snapshots = active_tests[test_id].setdefault("snapshots", [])
                            snapshots.append(snapshot)
                            if len(snapshots) > 200:
                                snapshots = snapshots[-200:]
                            active_tests[test_id]["snapshots"] = snapshots
                            active_tests[test_id]["intermediate"] = snapshot
                reader.close()

            tracker_thread = threading.Thread(target=tracker, daemon=True)
            tracker_thread.start()

            rc, rf = run_single_instance(test_id, script_path, results_file, str(ndjson_path))
            time_module.sleep(0.5)

            if test_id in active_tests:
                active_tests[test_id]["done"] = True
                active_tests[test_id]["returncode"] = rc
                if rf:
                    try:
                        active_tests[test_id]["summary"] = json.loads(rf.read_text(encoding="utf-8"))
                    except Exception as e:
                        active_tests[test_id]["error"] = f"Error: {e}"
                save_to_history(test_id, active_tests[test_id])
                fire_webhooks("completed" if rc == 0 else "failed", test_id, active_tests[test_id])
        finally:
            pass

    t = threading.Thread(target=run_normal, daemon=True)
    t.start()

    return {"test_id": test_id}


@app.route("/api/comparison/<comp_id>")
def api_comparison_result(comp_id):
    comp = comparisons.get(comp_id)
    if not comp:
        return jsonify({"error": "Comparativa no encontrada"}), 404

    test_a = active_tests.get(comp.get("test_a"), {})
    test_b = active_tests.get(comp.get("test_b"), {})

    return jsonify({
        "comp_id": comp_id,
        "done": comp.get("done"),
        "test_a": {
            "test_id": comp.get("test_a"),
            "target": test_a.get("target"),
            "done": test_a.get("done"),
            "summary": test_a.get("summary"),
            "snapshots": test_a.get("snapshots", []),
            "error": test_a.get("error"),
        },
        "test_b": {
            "test_id": comp.get("test_b"),
            "target": test_b.get("target"),
            "done": test_b.get("done"),
            "summary": test_b.get("summary"),
            "snapshots": test_b.get("snapshots", []),
            "error": test_b.get("error"),
        },
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load Tester - k6")
    parser.add_argument("--cli", action="store_true", help="Modo CLI (ejecutar y salir)")
    parser.add_argument("--url", help="URL objetivo")
    parser.add_argument("--type", default="login", help="Tipo de prueba")
    parser.add_argument("--users", type=int, default=10, help="Usuarios simultáneos")
    parser.add_argument("--output", default="json", choices=["json", "text"], help="Formato de salida")
    args = parser.parse_args()

    if args.cli:
        if not args.url:
            print("Error: --url es requerido en modo CLI")
            sys.exit(1)
        if not os.path.exists(K6_EXE):
            print(f"Error: k6 no encontrado en {K6_EXE}")
            sys.exit(1)

        test_id = uuid.uuid4().hex[:12]
        target_url = normalize_url(args.url)
        config = {
            "target_url": target_url,
            "test_type": args.type,
            "users": args.users,
            "login_endpoint": "/login",
            "upload_endpoint": "/upload",
        }
        params = build_params(config, args.type, target_url, args.users)
        script = generate_script(args.type, params)
        script_path = SCRIPTS_DIR / f"cli_{test_id}.js"
        script_path.write_text(script, encoding="utf-8")
        results_file = RESULTS_DIR / f"cli_{test_id}.json"

        print(f"Ejecutando test CLI: {args.users} usuarios contra {target_url}")
        rc, rf = run_single_instance(test_id, script_path, results_file)

        if rf:
            summary = json.loads(rf.read_text(encoding="utf-8"))
            if args.output == "json":
                print(json.dumps(summary, indent=2))
            else:
                m = summary.get("metrics", {})
                reqs = m.get("http_reqs", {})
                dur = m.get("http_req_duration", {})
                print(f"\nResultados:")
                print(f"  Requests: {reqs.get('count', 0)}")
                print(f"  Duración media: {dur.get('avg', 0):.1f}ms")
                print(f"  p95: {dur.get('p(95)', 0):.1f}ms")
                print(f"  Fallos: {m.get('http_req_failed', {}).get('rate', 0) * 100:.1f}%")
        sys.exit(0 if rc == 0 else 1)

    loaded = load_from_history()
    cleaned = cleanup_old_files()
    load_schedules()
    load_webhooks()

    scheduler_running = True
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    print(f"=== Load Tester ===")
    print(f"K6: {K6_EXE}")
    print(f"History cargados: {loaded}")
    print(f"Scripts viejos limpiados: {cleaned}")
    print(f"Máx concurrentes: {MAX_CONCURRENT_TESTS}")
    print(f"Schedules: {len(schedules)}")
    print(f"Webhooks: {len(webhooks)}")
    print(f"Abriendo http://127.0.0.1:5000")
    print(f"API docs: http://127.0.0.1:5000/api/docs")
    app.run(debug=False, host="127.0.0.1", port=5000)
