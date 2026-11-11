import json, os, random, time, copy
from concurrent.futures import ThreadPoolExecutor
from django.conf import settings
from google import genai
from google.genai import types
from ai.models import Concept
from ai.models.Concept import ConceptDecision
from utils.concept import judge as J, text

MODELS = os.environ.get('JB_MODELS', 'gemini-2.5-flash').split(',')
N = int(os.environ.get('JB_N', '120'))
random.seed(7)
rows = list(ConceptDecision.objects.exclude(candidates=[]).select_related('sense', 'concept').order_by('-created_at'))
seen, pool = set(), []
for d in rows:
    if d.sense_id in seen or not d.candidates:
        continue
    seen.add(d.sense_id); pool.append(d)
hard = [d for d in pool if d.outcome == 'ambiguous_split']
rest = [d for d in pool if d.outcome != 'ambiguous_split']
random.shuffle(rest)
cases = (hard + rest)[:N]
if os.environ.get('JB_ONLY') == 'hard_linked':
    cases = hard + [d for d in rest if d.outcome == 'linked']
prepared = []
for d in cases:
    cands = [{k: c.get(k, '') for k in ('id', 'label', 'pos', 'description')} for c in d.candidates[:6]]
    if not cands:
        continue
    s = d.sense
    info = {'head': text.head(s), 'pos': s.pos or '', 'definition': text.en_definition(s)}
    prompt = f"Sense: gloss \"{info['head']}\" [{info['pos']}]: {info['definition']}\nCandidates:\n{J._lines(cands)}"
    ref = str(d.concept_id) if d.outcome == 'linked' and d.concept_id else None
    prepared.append({'prompt': prompt, 'cands': cands, 'ref': ref, 'outcome': d.outcome, 'info': info})
print('cases', len(prepared), 'hard', sum(1 for p in prepared if p['outcome'] == 'ambiguous_split'), 'linked', sum(1 for p in prepared if p['ref']))
client = genai.Client(api_key=settings.GEMINI_API_KEY)

SCHEMA_USED = copy.deepcopy(J.SCHEMA)
if os.environ.get('JB_WHY') == '1':
    SCHEMA_USED['properties']['why'] = {'type': 'STRING'}; SCHEMA_USED['required'] = ['choice', 'near', 'why']

def ask(model, prompt):
    if model.startswith('claude'):
        from utils.ai import usage as U
        d = J._ask_claude(prompt)
        return d, J.LAST_USAGE
    cfgs = [types.ThinkingConfig(thinking_budget=0), None]
    last = None
    for tc in cfgs:
        try:
            kw = dict(system_instruction=J.SYSTEM, max_output_tokens=300, temperature=0, response_mime_type='application/json', response_schema=copy.deepcopy(SCHEMA_USED))
            if tc is not None:
                kw['thinking_config'] = tc
            r = client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(**kw))
            u = r.usage_metadata
            return json.loads(r.text), (u.prompt_token_count or 0, (u.candidates_token_count or 0) + (getattr(u, 'thoughts_token_count', 0) or 0))
        except Exception as e:
            last = e
            if '429' in str(e) or '503' in str(e):
                time.sleep(3)
    raise last

for model in MODELS:
    t = time.monotonic()
    def one(p):
        try:
            data, tok = ask(model, p['prompt'])
        except Exception as e:
            return None, (0, 0), str(e)[:80]
        ch = data.get('choice') or 0
        chosen = p['cands'][ch - 1]['id'] if 1 <= ch <= len(p['cands']) else None
        return chosen, tok, ''
    with ThreadPoolExecutor(6) as ex:
        out = list(ex.map(one, prepared))
    errs = sum(1 for o in out if o[2])
    agree = sum(1 for p, o in zip(prepared, out) if not o[2] and o[0] == p['ref'])
    over = sum(1 for p, o in zip(prepared, out) if not o[2] and o[0] and o[0] != p['ref'])  # nối trong khi tham chiếu không nối / nối chỗ khác
    under = sum(1 for p, o in zip(prepared, out) if not o[2] and p['ref'] and o[0] != p['ref'])
    hard_over = sum(1 for p, o in zip(prepared, out) if p['outcome'] == 'ambiguous_split' and o[0])
    tin = sum(o[1][0] for o in out); tout = sum(o[1][1] for o in out)
    print(f'## {model}: agree {agree}/{len(prepared)}  links-where-ref-differs {over}  misses {under}  hard-cases-linked {hard_over}  errors {errs}  tokens {tin}/{tout}  {time.monotonic()-t:.0f}s')
    if os.environ.get('JB_SHOW') == model:
        for p, o in zip(prepared, out):
            if o[2] or o[0] != p['ref']:
                lab = {c['id']: c['label'] for c in p['cands']}
                print(f"  - {p['info']['head']} [{p['info']['pos']}] {p['info']['definition'][:90]} | ref={lab.get(p['ref'])} ({p['outcome']}) model={lab.get(o[0])} {o[2]}")
