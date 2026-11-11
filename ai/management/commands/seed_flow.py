"""
Luồng seed hàng loạt theo step (ai/seedflow). Các lệnh con:

  make-step  --source <file> --dir <thư mục> --per-step N [--lang en] [--threshold 40]   tạo step_NNNN.jsonl kế tiếp
  lemmas     --source <file.jsonl> --out <file.jsonl> [--lang en] [--keep w1,w2] [--min-senses 8]   lọc dạng chia (went, killed, men...), chỉ giữ từ gốc có trong nguồn; ghi thêm <out>.dropped.tsv
  start      <run> --step-file <step.jsonl> --lang en --base-languages en,vi,zh,es,fr --mode batch|online
             online = luồng thường (gọi AI ngay, để thử); batch = luồng batch (Claude + Gemini Batch giảm 50%, chỉ concept online, để seed lớn)
  run        <run> [--until stage] [--no-wait] [--poll 30]                                chạy các giai đoạn còn lại
  export     <run> --dir D     ghi D/INSTRUCTIONS.md + D/<từ>.in.json cho agent ngoài (vd subagent Haiku) viết trọn dữ liệu từ (ai/seedflow/external.py)
  import     <run> --dir D     đọc D/<từ>.out.json, kiểm schema, dựng nghĩa vào trạng thái run (CHƯA ghi DB; kiểm bằng `report` rồi `run` mới save)
  export-verify  <run> --dir D [--redo]   agent ngoài kiểm bản dịch thay Haiku (đặt cờ để stage verify chờ); import-verify <run> --dir D nhận kết quả
  export-concept <run> --dir D            agent ngoài judge concept thay Gemini (embedding vẫn gọi API); import-concept <run> --dir D nhận kết quả
  status     <run>
  report     <run>                                                                       từng từ: nghĩa, bản dịch, vấn đề

Ví dụ:
  manage.py seed_flow make-step --source fixtures/wordlists/en_top1000_ref.jsonl --dir seed_runs/en_steps --per-step 20
  manage.py seed_flow start step1 --step-file seed_runs/en_steps/step_0001.jsonl --lang en --base-languages en,vi,zh,es,fr --mode online
  manage.py seed_flow run step1
"""
import json

from django.conf import settings

from django.core.management.base import BaseCommand, CommandError

from ai.seedflow import flow
from ai.seedflow.steps import MULTI_SENSE_THRESHOLD, make_step_file
from ai.validators import is_lang


class Command(BaseCommand):
    help = 'Luồng seed hàng loạt theo step: make-step, start, run, status, report.'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        p = sub.add_parser('make-step')
        p.add_argument('--source', required=True)
        p.add_argument('--dir', required=True)
        p.add_argument('--per-step', type=int, required=True)
        p.add_argument('--lang', default='en')
        p.add_argument('--step', type=int, default=None)
        p.add_argument('--threshold', type=int, default=MULTI_SENSE_THRESHOLD, help='Từ có từ ngần này nghĩa Wiktionary trở lên thì để file multi_sense.jsonl')
        p = sub.add_parser('lemmas')
        p.add_argument('--source', required=True)
        p.add_argument('--out', required=True)
        p.add_argument('--lang', default='en')
        p.add_argument('--min-senses', type=int, default=8, help='Dạng chia có từ ngần này nghĩa riêng ở nguồn trở lên thì giữ (0 = tắt)')
        p.add_argument('--keep', default='', help='Từ luôn giữ dù là dạng chia, cách nhau dấu phẩy (hoặc @file, mỗi dòng một từ)')
        p = sub.add_parser('start')
        p.add_argument('run')
        p.add_argument('--step-file', required=True)
        p.add_argument('--lang', default='en')
        p.add_argument('--base-languages', default=','.join(settings.TRANSLATE_LANGUAGES))
        p.add_argument('--mode', choices=['batch', 'online'], default='batch', help='online: luồng thường, mọi giai đoạn gọi ngay (thử); batch: luồng batch, chỉ concept online (seed lớn)')
        p = sub.add_parser('run')
        p.add_argument('run')
        p.add_argument('--until', choices=flow.STAGES)
        p.add_argument('--no-wait', action='store_true')
        p.add_argument('--poll', type=int, default=30)
        p = sub.add_parser('pipeline')
        p.add_argument('--runs', default='', help='Danh sách run đã start, cách nhau dấu phẩy (chạy lệch pha theo thứ tự)')
        p.add_argument('--source', help='Hoặc: tự tạo step kế tiếp từ file nguồn (cùng --dir / --per-step / --steps / --prefix)')
        p.add_argument('--dir')
        p.add_argument('--per-step', type=int, default=1000)
        p.add_argument('--steps', type=int, default=1, help='Số step tối đa sẽ tạo')
        p.add_argument('--prefix', default='p')
        p.add_argument('--lang', default='en')
        p.add_argument('--base-languages', default=','.join(settings.TRANSLATE_LANGUAGES))
        p.add_argument('--max-active', type=int, default=2)
        p.add_argument('--admit-after', choices=flow.STAGES, default='draft', help='Chỉ nhận step kế khi mọi step đang chạy đã qua stage này')
        p.add_argument('--poll', type=int, default=60)
        p = sub.add_parser('concept-worker')
        p.add_argument('--lang', required=True, help='Ngôn ngữ của nghĩa (mỗi ngôn ngữ một consumer)')
        p.add_argument('--once', action='store_true', help='Xử lý hết hàng rồi thoát (mặc định chờ việc mới tới khi Ctrl+C)')
        p.add_argument('--limit', type=int)
        p.add_argument('--idle', type=int, default=5)
        p.add_argument('--retry-failed', action='store_true', help='Đưa các nghĩa nối lỗi về cuối hàng trước khi chạy')
        sub.add_parser('concept-status')
        for name in ('export', 'import'):
            p = sub.add_parser(name)
            p.add_argument('run')
            p.add_argument('--dir', required=True)
        for name in ('export-verify', 'import-verify', 'export-concept', 'import-concept'):
            p = sub.add_parser(name)
            p.add_argument('run')
            p.add_argument('--dir', required=True)
            if name == 'export-verify':
                p.add_argument('--redo', action='store_true', help='Kiểm lại cả nghĩa đã có verify_state')
                p.add_argument('--no-flag', action='store_true', help='Không đặt cờ agent_verify (chỉ để đánh giá, không thay stage verify)')
        for name in ('status', 'report'):
            sub.add_parser(name).add_argument('run')

    def handle(self, *args, **opts):
        getattr(self, 'do_' + opts['action'].replace('-', '_'))(opts)

    def _run(self, name):
        run = flow.Run(name)
        if not run.state:
            raise CommandError(f'Chưa có run {name}')
        return run

    def do_make_step(self, o):
        try:
            info = make_step_file(o['source'], o['dir'], o['per_step'], language_code=o['lang'], step=o['step'], threshold=o['threshold'])
        except ValueError as e:
            raise CommandError(str(e))
        self.stdout.write(json.dumps(info, ensure_ascii=False))

    def do_lemmas(self, o):
        from ai.seedflow import lemmas
        keep = o['keep']
        if keep.startswith('@'):
            keep = ','.join(open(keep[1:], encoding='utf-8').read().split())
        rows = lemmas.read_rows(o['source'])
        kept, dropped, lexical = lemmas.split_forms(rows, o['lang'], keep=keep.split(','), min_senses=o['min_senses'])
        table = lemmas.write_outputs(kept, dropped, lexical, o['out'])
        self.stdout.write(f"Nguồn {len(rows)} dòng: giữ {len(kept)} (gồm {len(lexical)} dạng giữ vì có nghĩa riêng ở loại từ khác), loại {len(dropped)} dạng chia. Danh sách: {o['out']}; bảng bị loại: {table}")

    def do_start(self, o):
        base = [c.strip() for c in o['base_languages'].split(',') if c.strip()]
        if not is_lang(o['lang']) or not all(is_lang(c) for c in base):
            raise CommandError('--lang / --base-languages không hợp lệ')
        try:
            run = flow.Run.create(o['run'], o['step_file'], o['lang'], base, o['mode'])
        except ValueError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"Run {run.name}: {len(run.state['words'])} từ, mode {o['mode']}, thư mục {run.dir}"))

    def do_run(self, o):
        run = self._run(o['run'])
        waiting = flow.advance(run, until=o['until'], wait=not o['no_wait'], poll_seconds=o['poll'])
        self.do_status(o)
        if waiting:
            self.stdout.write(f'Đang chờ batch ở giai đoạn {waiting}; chạy lại lệnh run sau.')

    def do_pipeline(self, o):
        out = self.stdout.write
        if o['runs']:
            names = [n.strip() for n in o['runs'].split(',') if n.strip()]
            queue = iter(names)

            def next_run():
                name = next(queue, None)
                return self._run(name) if name else None
        else:
            if not o['source'] or not o['dir']:
                raise CommandError('Cần --runs hoặc --source + --dir')
            base = [c.strip() for c in o['base_languages'].split(',') if c.strip()]
            made = {'n': 0}

            def next_run():
                if made['n'] >= o['steps']:
                    return None
                try:
                    info = make_step_file(o['source'], o['dir'], o['per_step'], language_code=o['lang'])
                except ValueError as e:
                    out(f'pipeline: dừng tạo step: {e}')
                    return None
                made['n'] += 1
                name = f"{o['prefix']}{info['step']:04d}"
                existing = flow.Run(name)
                if existing.state:
                    return existing
                return flow.Run.create(name, f"{o['dir']}/{info['file']}", o['lang'], base, 'batch')
        result = flow.pipeline(next_run, max_active=o['max_active'], admit_after=o['admit_after'], poll_seconds=o['poll'], say=out)
        out(f"Pipeline xong: {result}")

    def do_concept_worker(self, o):
        from utils.concept import queue
        from utils.concept.linker import link_sense_id
        lang = o['lang']
        if o['retry_failed']:
            self.stdout.write(f'Đưa lại {queue.requeue_failed(lang)} nghĩa lỗi vào hàng')
        self.stdout.write(f'concept-worker {lang}: {queue.depth(lang)}')
        try:
            result = queue.consume(lang, link_sense_id, stop_when_empty=o['once'], idle_seconds=o['idle'], limit=o['limit'],
                                   on_item=lambda sid, outcome, n: self.stdout.write(f'  {n}. {sid} {outcome}') if n % 20 == 0 else None)
        except KeyboardInterrupt:
            self.stdout.write('Dừng; nghĩa đang làm dở sẽ được trả về hàng ở lần chạy sau.')
            return
        per = round(result['seconds'] / result['processed'], 2) if result['processed'] else 0
        self.stdout.write(f"Xong: {result['processed']} nghĩa trong {result['seconds']}s ({per}s / nghĩa) {result['outcomes']}")

    def do_concept_status(self, o):
        from utils.concept import queue
        langs = queue.languages()
        if not langs:
            self.stdout.write('Không có hàng concept nào.')
        for lang in langs:
            self.stdout.write(f'{lang}: {queue.depth(lang)}')

    def do_export(self, o):
        from ai.seedflow import external
        run = self._run(o['run'])
        if run.state['stages'].get('prepare') != 'done':
            raise CommandError('Chạy `run <run> --until prepare` trước (cần id từ)')
        self.stdout.write(f"Đã ghi {external.export_words(run, o['dir'])} từ vào {o['dir']} (đọc INSTRUCTIONS.md trước)")

    def do_import(self, o):
        from ai.seedflow import external
        run = self._run(o['run'])
        report = external.import_words(run, o['dir'], flow.sanitize_senses, flow.missing_enrichment)
        self.stdout.write(f"Nhập {len(report['imported'])} từ, từ chối {len(report['rejected'])}, chưa có file {len(report['missing'])}, sai {len(report['invalid'])}")
        for word, error in report['invalid']:
            self.stdout.write(f'  ! {word}: {error}')
        if report['missing']:
            self.stdout.write('  chưa có file ra: ' + ', '.join(report['missing']))
        self.stdout.write('Chưa ghi DB. Kiểm bằng `seed_flow report`, rồi `seed_flow run <run>` để save → verify → ảnh → concept.')

    def do_export_verify(self, o):
        from ai.seedflow import external
        run = self._run(o['run'])
        n = external.export_verify(run, o['dir'], redo=o['redo'], set_flag=not o['no_flag'])
        self.stdout.write(f"Đã ghi {n} từ cần kiểm vào {o['dir']} (<từ>.verify.in.json)")

    def do_import_verify(self, o):
        from ai.seedflow import external
        report = external.import_verify(self._run(o['run']), o['dir'])
        self.stdout.write(f"Nhập {report['ok']} từ, chưa có file {len(report['missing'])}, sai {len(report['invalid'])}")
        for word, error in report['invalid']:
            self.stdout.write(f'  ! {word}: {error}')
        self.stdout.write('Chạy `seed_flow run <run>` để stage verify áp kết quả.')

    def do_export_concept(self, o):
        from ai.seedflow import external
        n = external.export_concept(self._run(o['run']), o['dir'])
        self.stdout.write(f"Đã ghi {n} nghĩa cần judge vào {o['dir']} (<từ>.concept.in.json); nghĩa không có ứng viên sẽ tự tạo concept mới")

    def do_import_concept(self, o):
        from ai.seedflow import external
        report = external.import_concept(self._run(o['run']), o['dir'])
        self.stdout.write(f"Nhập {report['ok']} quyết định, chưa có file {len(report['missing'])}, sai {len(report['invalid'])}")
        for word, error in report['invalid']:
            self.stdout.write(f'  ! {word}: {error}')
        self.stdout.write('Chạy `seed_flow run <run>` để stage concept áp kết quả.')

    def do_status(self, o):
        run = self._run(o['run'])
        st = run.state
        counts = {}
        for info in st['words'].values():
            counts[info['status']] = counts.get(info['status'], 0) + 1
        self.stdout.write(f"Run {run.name} [{st['language_code']} -> {','.join(st['base_languages'])}, {st['mode']}]")
        self.stdout.write('Giai đoạn: ' + ', '.join(f"{s}={st['stages'].get(s, '-')}" for s in flow.STAGES))
        self.stdout.write(f'Từ: {counts}')
        if st.get('batch_progress'):
            self.stdout.write(f"Batch: {st['batch_progress']}")
        for stage, info in (st.get('gbatches') or {}).items():
            if info.get('provider') == 'claude':
                self.stdout.write(f"Claude batch {stage}: {info['batch_id']}, {info['requests']} request")
            else:
                self.stdout.write(f"Gemini batch {stage}: {len(info['jobs'])} job, {info['requests']} request")
        for name in ('enrich_batch', 'briefs_batch', 'concept_judge'):
            if st.get(name):
                self.stdout.write(f"{name}: {st[name]}")
        total = 0.0
        for stage, u in st['usage'].items():
            total += u['usd']
            self.stdout.write(f"  {stage}: {u['requests']} request, {u['in']} vào / {u['out']} ra, ~${u['usd']}")
        self.stdout.write(f'Chi phí Claude (chưa gồm Gemini, ảnh, concept): ~${round(total, 4)}')
        for issue in st['issues'][:20]:
            self.stdout.write(f"  ! {issue['word']} [{issue['stage']}]: {'; '.join(issue['missing'])}")

    def do_report(self, o):
        run = self._run(o['run'])
        lang, base = run.lang, run.state['base_languages']
        for word, info in run.state['words'].items():
            self.stdout.write(f"\n== {word} [{info['status']}] {info['reason']}")
            for s in run.senses.get(word, []):
                c = s.get('contents') or {}
                value = lambda node: (node or {}).get('value', '') if isinstance(node, dict) else (node or '')
                self.stdout.write(f"  - ({s.get('pos')}, {s.get('level')}) {value((c.get('definition') or {}).get(lang))}")
                for b in base:
                    if b != lang:
                        tr = (c.get('translations') or {}).get(b)
                        self.stdout.write(f"      {b}: {value((c.get('definition') or {}).get(b))}  | tr: {tr}")
