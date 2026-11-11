import asyncio, json, sys, time
from utils.ai import claude
from utils.ai.prompt import render_word_prompt, WORD_SYSTEM_PROMPT
from utils.ai.word_render import get_schema
from django.conf import settings

WORDS = sys.argv[1:] if False else None
import os
words = os.environ.get('AB_WORDS', 'run').split(',')
refs = {}
for line in open('fixtures/wordlists/en_top1000_ref.jsonl', encoding='utf-8'):
    r = json.loads(line); refs[r['w']] = r['entries']
schema = get_schema('latin')
sem = asyncio.Semaphore(5)

async def call(word, ref):
    async with sem, claude.client() as client:
        t = time.monotonic()
        resp = await client.messages.create(
            model=settings.AI_DRAFT_MODELS[0], max_tokens=8192, system=WORD_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': render_word_prompt(word, 'en', ref)}],
            thinking={'type': 'disabled'}, output_config=claude.output_config(schema))
        text = next(b.text for b in resp.content if b.type == 'text')
        return json.loads(text), resp.usage.input_tokens, resp.usage.output_tokens, time.monotonic() - t

def senses(data):
    return [(s.get('pos'), s.get('level'), (s.get('definition') or {}).get('value')) for s in data.get('senses', [])]

async def main():
    jobs = []
    for w in words:
        jobs += [call(w, None), call(w, refs.get(w))]
    res = await asyncio.gather(*jobs)
    tin = tout = 0
    for i, w in enumerate(words):
        a, b = res[2 * i], res[2 * i + 1]
        tin += a[1] + b[1]; tout += a[2] + b[2]
        print(f'=== {w}  (ref entries: {[e["pos"] for e in refs.get(w, [])]})')
        for tag, r in (('A no-ref', a), ('B ref   ', b)):
            print(f'  {tag} [{r[1]}in/{r[2]}out {r[3]:.1f}s]')
            for pos, lv, d in senses(r[0]):
                print(f'     - {pos} {lv}: {d}')
    print('TOKENS', tin, tout, 'approx $', round(tin * 0.10 / 1e6 + tout * 0.50 / 1e6, 4))

asyncio.run(main())
