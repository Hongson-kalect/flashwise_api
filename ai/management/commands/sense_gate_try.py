"""
Chạy thử cổng AI duyệt nghĩa mới (utils/ai/sense_gate.py) trên các đề xuất trong file JSON, dùng nghĩa có sẵn trong DB làm ngữ cảnh.
Không ghi gì vào DB. Ví dụ:
  python manage.py sense_gate_try fixtures/sense_proposals_try.json
  python manage.py sense_gate_try fixtures/sense_proposals_try.json --all-pos --no-layer2
"""
import asyncio
import json

from django.core.management.base import BaseCommand

from ai.models import AISense, AIWord
from utils.ai import sense_gate


def existing_senses(word, language):
    rows = []
    for sense in AISense.objects.filter(word__value__iexact=word, word__language_code=language, is_official=True) \
            .exclude(is_deleted=True).order_by('id'):
        contents = sense.contents or {}
        rows.append({
            'pos': sense.pos,
            'definition': (contents.get('definition') or {}).get(language, {}).get('value', ''),
            'example': (((contents.get('examples') or [{}])[0]).get(language) or {}).get('value', ''),
        })
    return rows


async def run_one(case, same_pos_only, layer2):
    prompt, shown = sense_gate.render_gate_prompt(
        case['word'], case['language'], case.get('pos'), case['existing'], case['definition'], same_pos_only=same_pos_only,
    )
    jobs = [sense_gate.gate_claude(prompt)]
    if layer2:
        jobs.append(sense_gate.gate_gemini(prompt))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    return shown, results


class Command(BaseCommand):
    help = 'Chạy thử cổng AI duyệt nghĩa mới trên các đề xuất mẫu (không ghi DB)'

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--all-pos', action='store_true', help='Gửi nghĩa mọi loại từ làm ngữ cảnh (mặc định chỉ cùng loại từ)')
        parser.add_argument('--no-layer2', action='store_true', help='Chỉ chạy lớp 1 (Claude)')
        parser.add_argument('--expand', action='store_true', help='Nghĩa được accept ở lớp 1 thì sinh nốt nội dung + bản dịch + metadata')
        parser.add_argument('--user-lang', default='vi', help='Ngôn ngữ dịch khi --expand / --single (mặc định vi)')
        parser.add_argument('--single', action='store_true', help='Luồng 1 lần gọi: Claude chấm + viết đủ nghĩa; Gemini chỉ kiểm chéo khi accept')
        parser.add_argument('--min-confidence', type=float, default=0.6)
        parser.add_argument('--show', action='store_true', help='In nội dung đã sinh cho ca accept')

    def handle(self, *args, **options):
        with open(options['path'], encoding='utf-8') as f:
            cases = json.load(f)
        if options['single']:
            return self.handle_single(cases, options)
        same_pos_only = not options['all_pos']
        layer2 = not options['no_layer2']
        self.stdout.write(f'Ngữ cảnh: {"cùng loại từ" if same_pos_only else "mọi loại từ"} | lớp 2 (Gemini): {"bật" if layer2 else "tắt"}\n')

        for case in cases:
            if not AIWord.objects.filter(value__iexact=case['word'], language_code=case['language']).exists():
                self.stdout.write(self.style.WARNING(f'{case["case"]}: từ "{case["word"]}" chưa có trong DB, bỏ qua'))
                continue
            case['existing'] = existing_senses(case['word'], case['language'])
            shown, results = asyncio.run(run_one(case, same_pos_only, layer2))
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{case["case"]}  [{case["word"]} / {case.get("pos")}]  (kỳ vọng: {case["expect"]})'))
            self.stdout.write(f'  đề xuất: {case["definition"][:110]}')
            self.stdout.write(f'  ngữ cảnh gửi: {len(shown)}/{len(case["existing"])} nghĩa có sẵn')
            layers = []
            for name, res in zip(('lớp 1 Claude', 'lớp 2 Gemini'), results):
                if isinstance(res, Exception):
                    self.stdout.write(self.style.ERROR(f'  {name}: LỖI {type(res).__name__}: {res}'))
                    layers.append(None)
                    continue
                layers.append(res)
                flags = [k for k in ('injection_attempt', 'is_offensive_or_spam') if res[k]]
                dup = f' trùng #{res["duplicate_of"]}' if res['duplicate_of'] else ''
                self.stdout.write(
                    f'  {name} ({res["model"]}): {res["verdict"].upper()}{dup} conf={res["confidence"]} accurate={res["is_accurate"]} '
                    f'pos_ok={res["pos_correct"]}{" flags=" + ",".join(flags) if flags else ""}  '
                    f'[{res["seconds"]}s, {res["tokens"]["in"]} in / {res["tokens"]["out"]} out]'
                )
                self.stdout.write(f'      lý do: {res["reason"]}')
            if layers[0]:
                final = sense_gate.decide(layers[0], layers[1] if len(layers) > 1 else None)
                self.stdout.write(self.style.SUCCESS(f'  => QUYẾT ĐỊNH: {final}'))
                if options['expand'] and layers[0]['verdict'] == 'accept':
                    self.show_expand(case, options['user_lang'])

    def show_expand(self, case, user_lang):
        prompt = sense_gate.render_expand_prompt(case['word'], case['language'], case.get('pos'), case['existing'], case['definition'])

        async def both():
            expanded = await sense_gate.expand_claude(prompt, case['language'], user_lang)
            finished = await sense_gate.finish_gemini(case['word'], case['language'], expanded['sense'])
            return expanded, finished

        expanded, finished = asyncio.run(both())
        result = sense_gate.assemble_sense(case['language'], user_lang, expanded['sense'], expanded['translation'], finished['finish'])
        problem = expanded['translation_problem']
        self.stdout.write(
            f'  [sinh nội dung] Claude {expanded["seconds"]}s ({expanded["tokens"]["in"]} in / {expanded["tokens"]["out"]} out, {expanded["attempts"]} lần, '
            f'dịch {"LỖI: " + problem if problem else "OK"}) + Gemini metadata {finished["seconds"]}s ({finished["tokens"]["in"]} in / {finished["tokens"]["out"]} out)'
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))

    def handle_single(self, cases, options):
        user_lang = options['user_lang']
        self.stdout.write(f'Luồng 1 lần gọi (Claude chấm + viết), Gemini chỉ kiểm chéo khi accept và conf > {options["min_confidence"]}\n')
        totals = {'seconds': 0.0, 'in': 0, 'out': 0}
        for case in cases:
            case['existing'] = existing_senses(case['word'], case['language'])
            prompt, _ = sense_gate.render_propose_prompt(case['word'], case['language'], user_lang, case.get('pos'), case['existing'], case['definition'])

            async def go():
                first = await sense_gate.propose_claude(prompt, case['language'], user_lang)
                second = None
                started = None
                if first['verdict'] == 'accept' and first['confidence'] > options['min_confidence']:
                    gate_prompt, _ = sense_gate.render_gate_prompt(case['word'], case['language'], case.get('pos'), case['existing'], case['definition'])
                    second = await sense_gate.gate_gemini(gate_prompt)
                return first, second

            first, second = asyncio.run(go())
            final = sense_gate.decide_single(first, second, options['min_confidence'])
            gem_seconds = second['seconds'] if second else 0
            total = round(first['seconds'] + gem_seconds, 1)
            totals['seconds'] += total
            totals['in'] += first['tokens']['in']
            totals['out'] += first['tokens']['out']
            flags = [k for k in ('injection_attempt', 'is_offensive_or_spam') if first[k]]
            empty = not first['sense']['definition']['value'].strip()
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{case["case"]}  [{case["word"]}]  (kỳ vọng: {case["expect"]})'))
            self.stdout.write(
                f'  Claude: {first["verdict"].upper()} conf={first["confidence"]}{" flags=" + ",".join(flags) if flags else ""}  '
                f'nội dung {"RỖNG" if empty else "có"}  [{first["seconds"]}s, {first["tokens"]["in"]} in / {first["tokens"]["out"]} out]  '
                f'dịch {"LỖI: " + first["translation_problem"] if first["translation_problem"] else ("OK" if not empty else "-")}'
            )
            self.stdout.write(f'  Gemini kiểm chéo: {second["verdict"].upper() + " [" + str(second["seconds"]) + "s]" if second else "không gọi"}')
            self.stdout.write(self.style.SUCCESS(f'  => QUYẾT ĐỊNH: {final}   (tổng {total}s)'))
            if options['show'] and not empty:
                result = sense_gate.assemble_sense(case['language'], user_lang, first['sense'], first['sense']['translation'], first['sense']['metadata'])
                m = result['metadata']
                c_ = result['contents']
                self.stdout.write(f'     VI: {c_["definition"].get(user_lang, {}).get("value", "")}')
                self.stdout.write(f'     translations: {c_.get("translations", {}).get(user_lang)} | level {result["level"]} | ipa {[i["value"] for i in result["ipas"]]}')
                self.stdout.write(f'     tags {m["tags"]} | keywords {m["image_keywords"]} | adv {m["advanced"]}')
        self.stdout.write(f'\nTổng: {round(totals["seconds"], 1)}s, {totals["in"]} token vào, {totals["out"]} token ra cho {len(cases)} ca')
