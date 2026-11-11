"""
Gemini Batch API (giảm 50% giá, xong trong vài phút đến 24 giờ) cho luồng seed.

Luồng thường gọi Gemini ngay (translate.py, metadata.py). Để chạy bằng batch mà không viết lại logic dựng prompt và áp kết quả, các hàm
đó hỏi `intercept()` trước khi gọi thật:

  - không có hook (luồng thường)      -> intercept trả None, gọi Gemini online như cũ
  - hook Recorder (lượt ghi)           -> ghi lại request (model, prompt, schema), trả `{}` để hàm chạy tiếp mà không đổi gì
  - hook Replayer (lượt phát lại)      -> trả kết quả batch đã có cho đúng request (tra theo khoá băm); request không có kết quả
                                          (batch lỗi, JSON hỏng) thì trả None nên hàm tự gọi online như bình thường

Quy trình một giai đoạn batch (ai/seedflow/flow.py#_gemini_phase): chạy hàm với Recorder -> `submit()` -> chờ `poll()` -> lưu kết quả ->
chạy lại hàm với Replayer. Vì cùng một mã dựng prompt chạy ở cả hai lượt nên khoá băm khớp nhau.
"""
import contextlib
import contextvars
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request

from django.conf import settings

from utils.ai import usage

logger = logging.getLogger(__name__)

BASE = 'https://generativelanguage.googleapis.com/v1beta/'
MAX_PER_JOB = 400  # số request tối đa một job (inline tối đa 20MB; mỗi request vài KB)
DONE_STATES = ('SUCCEEDED',)
DEAD_STATES = ('FAILED', 'CANCELLED', 'EXPIRED')

_hook = contextvars.ContextVar('gemini_batch_hook', default=None)


def thinking_config(model):
    """Tắt suy nghĩ, cùng quy tắc với translate.thinking_off nhưng ở dạng REST: 2.5 dùng thinkingBudget=0, 3.x dùng thinkingLevel=minimal."""
    if str(model).startswith('gemini-2.5'):
        return {'thinkingBudget': 0}
    return {'thinkingLevel': 'minimal'}


def request_key(model, contents, schema, system=None):
    payload = json.dumps([model, contents, schema, system or ''], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:24]


def build_request(model, contents, schema, system=None, max_tokens=8192):
    config = {'maxOutputTokens': max_tokens, 'responseMimeType': 'application/json', 'responseSchema': schema,
              'thinkingConfig': thinking_config(model)}
    request = {'contents': [{'parts': [{'text': contents}]}], 'generationConfig': config}
    if system:
        request['systemInstruction'] = {'parts': [{'text': system}]}
    return request


# ----------------------------------------------------------------- hook

class Recorder:
    """Lượt ghi: gom request, không gọi Gemini."""

    def __init__(self):
        self.requests = {}  # khoá -> {'label', 'model', 'request'}

    def handle(self, label, model, contents, schema, system, max_tokens):
        key = request_key(model, contents, schema, system)
        self.requests.setdefault(key, {'label': label, 'model': model, 'request': build_request(model, contents, schema, system, max_tokens),
                                       'contents': contents, 'schema': schema, 'system': system, 'max_tokens': max_tokens})
        return {}


class Replayer:
    """Lượt phát lại: trả kết quả batch theo khoá. results: {khoá: {'text', 'in', 'out'}}."""

    def __init__(self, results):
        self.results = results or {}
        self.hits = 0
        self.misses = 0

    def handle(self, label, model, contents, schema, system, max_tokens):
        item = self.results.get(request_key(model, contents, schema, system))
        if item is None:
            self.misses += 1
            return None
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError, KeyError):
            self.misses += 1
            return None
        if not isinstance(data, dict):
            self.misses += 1
            return None
        usage._add(f'{label}_batch', item.get('provider', 'gemini'), item.get('in', 0), item.get('out', 0))
        self.hits += 1
        return data


@contextlib.contextmanager
def use(hook):
    token = _hook.set(hook)
    try:
        yield hook
    finally:
        _hook.reset(token)


async def intercept(label, model, contents, schema, system=None, max_tokens=8192):
    """Dữ liệu (dict) từ hook, hoặc None = hãy gọi Gemini online như thường."""
    hook = _hook.get()
    if hook is None:
        return None
    return hook.handle(label, model, contents, schema, system, max_tokens)


# ----------------------------------------------------------------- REST

def _http(method, path, body=None, attempts=4):
    url = BASE + path + ('&' if '?' in path else '?') + 'key=' + settings.GEMINI_API_KEY
    data = json.dumps(body).encode('utf-8') if body is not None else None
    for attempt in range(attempts):
        req = urllib.request.Request(url, method=method, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f'Gemini batch {method} {path}: {e.code} {detail}')
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == attempts - 1:
                raise RuntimeError(f'Gemini batch {method} {path}: {e}')
            time.sleep(5 * (attempt + 1))


def submit(recorded, name):
    """Gửi các request đã ghi (Recorder.requests) thành một hoặc nhiều job. Trả [{'name', 'keys'}]."""
    items = list(recorded.items())
    jobs = []
    for part, start in enumerate(range(0, len(items), MAX_PER_JOB), 1):
        chunk = items[start:start + MAX_PER_JOB]
        by_model = {}
        for key, entry in chunk:  # một job chỉ có một model
            by_model.setdefault(entry['model'], []).append((key, entry))
        for model, group in by_model.items():
            body = {'batch': {'display_name': f'{name}_{part}_{model}'[:120],
                              'input_config': {'requests': {'requests': [{'request': e['request'], 'metadata': {'key': k}} for k, e in group]}}}}
            r = _http('POST', f'models/{model}:batchGenerateContent', body)
            job = r.get('name') or (r.get('metadata') or {}).get('name')
            if not job:
                raise RuntimeError(f'Gemini batch không trả tên job: {json.dumps(r)[:300]}')
            jobs.append({'name': job, 'keys': [k for k, _ in group], 'model': model})
    return jobs


def _state(job):
    return str((job.get('metadata') or {}).get('state') or job.get('state') or '')


def poll(job):
    """(trạng thái: 'done' | 'running' | 'dead', {khoá: {'text','in','out'}} khi done)."""
    j = _http('GET', job['name'])
    state = _state(j)
    if j.get('done') and j.get('error'):
        return 'dead', {}
    if any(s in state for s in DEAD_STATES):
        return 'dead', {}
    if not (j.get('done') or any(s in state for s in DONE_STATES)):
        return 'running', {}
    response = j.get('response') or {}
    inline = (response.get('inlinedResponses') or {}).get('inlinedResponses') or response.get('inlinedResponses') or []
    results = {}
    for position, item in enumerate(inline):
        key = (item.get('metadata') or {}).get('key') or (job['keys'][position] if position < len(job['keys']) else None)
        try:
            r = item['response']
            text = ''.join(p.get('text', '') for p in r['candidates'][0]['content']['parts'])
            u = r.get('usageMetadata') or {}
            results[key] = {'text': text, 'in': u.get('promptTokenCount', 0),
                            'out': (u.get('candidatesTokenCount', 0) or 0) + (u.get('thoughtsTokenCount', 0) or 0)}
        except (KeyError, IndexError, TypeError):
            logger.warning('Gemini batch: request %s không có kết quả dùng được: %s', key, json.dumps(item)[:200])
    return 'done', results
