"""
Luồng seed hàng loạt theo step (ai/seedflow). Các lệnh con:

  make-step  --source <file> --dir <thư mục> --per-step N [--lang en] [--threshold 50]   tạo step_NNNN.jsonl kế tiếp
  start      <run> --step-file <step.jsonl> --lang en --base-languages en,vi --mode batch|online
  run        <run> [--until stage] [--no-wait] [--poll 30]                                chạy các giai đoạn còn lại
  status     <run>
  report     <run>                                                                       từng từ: nghĩa, bản dịch, vấn đề

Ví dụ:
  manage.py seed_flow make-step --source fixtures/wordlists/en_top1000_ref.jsonl --dir seed_runs/en_steps --per-step 20
  manage.py seed_flow start step1 --step-file seed_runs/en_steps/step_0001.jsonl --lang en --base-languages en,vi --mode online
  manage.py seed_flow run step1
"""
import json

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
        p = sub.add_parser('start')
        p.add_argument('run')
        p.add_argument('--step-file', required=True)
        p.add_argument('--lang', default='en')
        p.add_argument('--base-languages', default='en,vi')
        p.add_argument('--mode', choices=['batch', 'online'], default='batch')
        p = sub.add_parser('run')
        p.add_argument('run')
        p.add_argument('--until', choices=flow.STAGES)
        p.add_argument('--no-wait', action='store_true')
        p.add_argument('--poll', type=int, default=30)
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
