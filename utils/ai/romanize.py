"""
Phiên âm Latin cho tiếng Nhật / Trung / Hàn bằng thuật toán, không nhờ AI:
- ja: Hepburn (có dấu gạch ngang trên nguyên âm dài: ō ū ā ē), dựa vào fugashi (có sẵn cho furigana).
- zh: Hán ngữ bính âm (pinyin) có dấu thanh, bằng pypinyin.
- ko: Revised Romanization (có biến âm: nối âm, mũi hoá, ngạc hoá, bật hơi), tự cài đặt vì thư viện có sẵn bỏ sót
  nhiều quy tắc (감사합니다 → gamsahapnida, 독립 → dokrip).
`add_readings` gắn `roman` (và `ruby` cho tiếng Nhật) vào definition / usage / examples; `reading_ipas` dựng trường ipas
(label READING, value = cách đọc theo chữ của ngôn ngữ đó, roman = phiên âm Latin).
"""
import logging
import re
import unicodedata

from utils.ai.japanese import add_ruby

logger = logging.getLogger(__name__)

ROMAN_LANGS = ('ja', 'zh', 'ko')

HANZI = re.compile(r'[㐀-䶿一-鿿]')
HANGUL = re.compile(r'[가-힣]')
HANGUL_RUN = re.compile(r'[가-힣]+')
KANA = re.compile(r'[぀-ヿ]')
KANA_ONLY = re.compile(r'^[぀-ゟ゠-ヿー]+$')
SCRIPT = {'ja': re.compile(r'[぀-ヿ㐀-䶿一-鿿]'), 'zh': HANZI, 'ko': HANGUL}


def _lang(language_code):
    return (language_code or '').split('-')[0].lower()


def has_script(text, language_code):
    pattern = SCRIPT.get(_lang(language_code))
    return bool(pattern and text and pattern.search(text))


# ============================== Tiếng Trung (pinyin) ==============================

_ZH_PUNCT = str.maketrans({
    '。': '.', '，': ',', '、': ',', '？': '?', '！': '!', '：': ':', '；': ';',
    '（': '(', '）': ')', '《': '“', '》': '”', '「': '“', '」': '”', '…': '...', '—': '-',
})
_CLOSERS = set('.,?!:;)”’')
_OPENERS = set('(“‘')
_VOWEL_START = re.compile(r"^[aeoāáǎàēéěèōóǒò]")
_ZH_TOKEN = re.compile(r'[㐀-䶿一-鿿]+|[A-Za-z0-9]+(?:[.,][0-9]+)?|\s+|\.\.\.|.')


def romanize_zh(text, as_word=False):
    """
    Pinyin có dấu thanh. as_word=True (từ ngắn): các âm tiết viết liền (yínháng, kèm dấu ' khi cần: xī'ān);
    ngược lại mỗi âm tiết cách nhau một dấu cách. Dấu câu đổi sang dạng Latin, dấu cách theo lệ thường.
    """
    from pypinyin import Style, pinyin
    text = (text or '').translate(_ZH_PUNCT)
    tokens = []  # (chuỗi, loại)
    for part in _ZH_TOKEN.findall(text):
        if HANZI.match(part):
            for chunk in pinyin(part, style=Style.TONE, errors=lambda s: [s], heteronym=False):
                tokens.append((chunk[0], 'syl'))
        elif part.isspace():
            continue
        elif part in _CLOSERS or part == '...':
            tokens.append((part, 'close'))
        elif part in _OPENERS:
            tokens.append((part, 'open'))
        else:
            tokens.append((part, 'other'))
    out = ''
    prev = None
    for item, kind in tokens:
        if out and kind != 'close' and prev != 'open':
            if kind == 'syl' and prev == 'syl' and as_word:
                if _VOWEL_START.match(item):
                    out += "'"
            else:
                out += ' '
        out += item
        prev = kind
    return out


def zh_is_word(text):
    """Từ ngắn (≤ 3 chữ Hán, không có dấu cách) viết pinyin liền; cụm dài tách từng âm tiết."""
    text = (text or '').strip()
    return len(HANZI.findall(text)) <= 3 and ' ' not in text


# ============================== Tiếng Hàn (Revised Romanization) ==============================

_KO_INITIALS = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ'
_KO_VOWELS = 'ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ'
_KO_FINALS = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ', 'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ',
              'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']
_KO_SPLIT = {'ㄳ': ['ㄱ', 'ㅅ'], 'ㄵ': ['ㄴ', 'ㅈ'], 'ㄶ': ['ㄴ', 'ㅎ'], 'ㄺ': ['ㄹ', 'ㄱ'], 'ㄻ': ['ㄹ', 'ㅁ'],
             'ㄼ': ['ㄹ', 'ㅂ'], 'ㄽ': ['ㄹ', 'ㅅ'], 'ㄾ': ['ㄹ', 'ㅌ'], 'ㄿ': ['ㄹ', 'ㅍ'], 'ㅀ': ['ㄹ', 'ㅎ'], 'ㅄ': ['ㅂ', 'ㅅ']}
# Âm đại diện của patchim đơn khi đứng cuối âm tiết / trước phụ âm
_KO_REP = {'ㄱ': 'ㄱ', 'ㄲ': 'ㄱ', 'ㅋ': 'ㄱ', 'ㄷ': 'ㄷ', 'ㅅ': 'ㄷ', 'ㅆ': 'ㄷ', 'ㅈ': 'ㄷ', 'ㅊ': 'ㄷ', 'ㅌ': 'ㄷ', 'ㅎ': 'ㄷ',
           'ㅂ': 'ㅂ', 'ㅍ': 'ㅂ', 'ㄴ': 'ㄴ', 'ㄹ': 'ㄹ', 'ㅁ': 'ㅁ', 'ㅇ': 'ㅇ'}
# Patchim kép: giữ phụ âm đầu (ㄳ ㄵ ㄶ ㄽ ㄼ ㄾ ㅀ ㅄ) hay phụ âm sau (ㄺ→ㄱ, ㄻ→ㅁ, ㄿ→ㅂ)
_KO_REP_DOUBLE = {'ㄳ': 'ㄱ', 'ㄵ': 'ㄴ', 'ㄶ': 'ㄴ', 'ㄺ': 'ㄱ', 'ㄻ': 'ㅁ', 'ㄼ': 'ㄹ', 'ㄽ': 'ㄹ', 'ㄾ': 'ㄹ', 'ㄿ': 'ㅂ',
                  'ㅀ': 'ㄹ', 'ㅄ': 'ㅂ'}
_KO_REP_PAIR = {''.join(v): _KO_REP_DOUBLE[k] for k, v in _KO_SPLIT.items()}
_KO_ASPIRATE = {'ㄱ': 'ㅋ', 'ㄲ': 'ㅋ', 'ㅋ': 'ㅋ', 'ㄷ': 'ㅌ', 'ㅅ': 'ㅌ', 'ㅆ': 'ㅌ', 'ㅌ': 'ㅌ', 'ㅈ': 'ㅊ', 'ㅊ': 'ㅊ',
                'ㅂ': 'ㅍ', 'ㅍ': 'ㅍ'}
_KO_INITIAL_ROMAN = {'ㄱ': 'g', 'ㄲ': 'kk', 'ㄴ': 'n', 'ㄷ': 'd', 'ㄸ': 'tt', 'ㄹ': 'r', 'ㅁ': 'm', 'ㅂ': 'b', 'ㅃ': 'pp',
                     'ㅅ': 's', 'ㅆ': 'ss', 'ㅇ': '', 'ㅈ': 'j', 'ㅉ': 'jj', 'ㅊ': 'ch', 'ㅋ': 'k', 'ㅌ': 't', 'ㅍ': 'p', 'ㅎ': 'h'}
_KO_VOWEL_ROMAN = {'ㅏ': 'a', 'ㅐ': 'ae', 'ㅑ': 'ya', 'ㅒ': 'yae', 'ㅓ': 'eo', 'ㅔ': 'e', 'ㅕ': 'yeo', 'ㅖ': 'ye', 'ㅗ': 'o',
                   'ㅘ': 'wa', 'ㅙ': 'wae', 'ㅚ': 'oe', 'ㅛ': 'yo', 'ㅜ': 'u', 'ㅝ': 'wo', 'ㅞ': 'we', 'ㅟ': 'wi', 'ㅠ': 'yu',
                   'ㅡ': 'eu', 'ㅢ': 'ui', 'ㅣ': 'i'}
_KO_FINAL_ROMAN = {'ㄱ': 'k', 'ㄴ': 'n', 'ㄷ': 't', 'ㄹ': 'l', 'ㅁ': 'm', 'ㅂ': 'p', 'ㅇ': 'ng'}


def _ko_syllables(run):
    result = []
    for ch in run:
        code = ord(ch) - 0xAC00
        fin = _KO_FINALS[code % 28]
        result.append({'ini': _KO_INITIALS[code // 588], 'vow': _KO_VOWELS[(code % 588) // 28],
                       'fin': _KO_SPLIT.get(fin, [fin]) if fin else []})
    return result


def _ko_final_sound(fin, next_initial):
    """Âm đại diện của patchim (đơn hoặc kép) khi đứng trước phụ âm."""
    if len(fin) == 2:
        if fin == ['ㄹ', 'ㄱ'] and next_initial == 'ㄱ':
            return 'ㄹ'
        return _KO_REP_PAIR[''.join(fin)]
    return _KO_REP[fin[0]]


def _ko_apply_rules(syls):
    """Biến âm trong một từ (cụm không có dấu cách): nối âm, ngạc hoá, bật hơi, mũi hoá, lỏng hoá. Sửa tại chỗ."""
    for i in range(len(syls) - 1):
        a, b = syls[i], syls[i + 1]
        fin, nxt = a['fin'], b['ini']
        if not fin:
            continue
        if nxt == 'ㅇ':  # nối âm
            if fin == ['ㅇ']:
                continue
            if fin == ['ㅎ']:
                a['fin'] = []
            elif len(fin) == 2:
                if fin[1] == 'ㅎ':
                    a['fin'], b['ini'] = [], fin[0]
                else:
                    a['fin'], b['ini'] = [fin[0]], fin[1]
            else:
                a['fin'] = []
                if b['vow'] == 'ㅣ' and fin[0] in ('ㄷ', 'ㅌ'):  # ngạc hoá: 같이 → gachi
                    b['ini'] = 'ㅈ' if fin[0] == 'ㄷ' else 'ㅊ'
                else:
                    b['ini'] = fin[0]
            continue
        if nxt == 'ㅎ':  # bật hơi: 국화 → gukwa, 닫히다 → dachida
            last = fin[-1]
            if len(fin) == 1 and b['vow'] == 'ㅣ' and last in ('ㄷ', 'ㅌ'):
                a['fin'], b['ini'] = [], 'ㅊ'
                continue
            if last in _KO_ASPIRATE:
                a['fin'] = fin[:-1]
                b['ini'] = _KO_ASPIRATE[last]
                if a['fin']:
                    a['fin'] = [_ko_final_sound(a['fin'], 'ㅎ')]
                continue
        if fin[-1] == 'ㅎ':  # 좋고 → joko, 많다 → manta
            if nxt in ('ㄱ', 'ㄷ', 'ㅈ'):
                b['ini'] = {'ㄱ': 'ㅋ', 'ㄷ': 'ㅌ', 'ㅈ': 'ㅊ'}[nxt]
                a['fin'] = fin[:-1]
                if a['fin']:
                    a['fin'] = [_ko_final_sound(a['fin'], nxt)]
                continue
            if len(fin) == 2:
                fin = a['fin'] = fin[:-1]
        rep = _ko_final_sound(fin, nxt)
        nxt = b['ini']
        if nxt == 'ㄹ':
            if rep in ('ㅁ', 'ㅇ', 'ㄱ', 'ㅂ', 'ㄷ'):
                b['ini'] = nxt = 'ㄴ'
            elif rep == 'ㄴ':
                rep = 'ㄹ'
        elif rep == 'ㄹ' and nxt == 'ㄴ':
            b['ini'] = nxt = 'ㄹ'
        if nxt in ('ㄴ', 'ㅁ'):
            rep = {'ㄱ': 'ㅇ', 'ㄷ': 'ㄴ', 'ㅂ': 'ㅁ'}.get(rep, rep)
        a['fin'] = [rep]


def _ko_render(syls):
    out = ''
    prev_fin = ''
    for s in syls:
        ini = _KO_INITIAL_ROMAN[s['ini']]
        if s['ini'] == 'ㄹ' and prev_fin == 'ㄹ':
            ini = 'l'
        fin = ''
        rep = None
        if s['fin']:
            rep = _ko_final_sound(s['fin'], '')
            fin = _KO_FINAL_ROMAN[rep]
        out += ini + _KO_VOWEL_ROMAN[s['vow']] + fin
        prev_fin = rep or ''
    return out


def romanize_ko(text):
    """Revised Romanization theo cách đọc thật (không phiên từng chữ). Biến âm chỉ xét trong một từ (giữa hai dấu cách thì không)."""
    return HANGUL_RUN.sub(lambda m: _ko_romanize_run(m.group(0)), text or '')


def _ko_romanize_run(run):
    syls = _ko_syllables(run)
    _ko_apply_rules(syls)
    return _ko_render(syls)


# ============================== Tiếng Nhật (Hepburn) ==============================

_KATA_TO_HIRA = {chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)}
_HIRA_TO_KATA = {v: k for k, v in _KATA_TO_HIRA.items()}

_JA_BASE = {
    'ア': 'a', 'イ': 'i', 'ウ': 'u', 'エ': 'e', 'オ': 'o',
    'カ': 'ka', 'キ': 'ki', 'ク': 'ku', 'ケ': 'ke', 'コ': 'ko',
    'ガ': 'ga', 'ギ': 'gi', 'グ': 'gu', 'ゲ': 'ge', 'ゴ': 'go',
    'サ': 'sa', 'シ': 'shi', 'ス': 'su', 'セ': 'se', 'ソ': 'so',
    'ザ': 'za', 'ジ': 'ji', 'ズ': 'zu', 'ゼ': 'ze', 'ゾ': 'zo',
    'タ': 'ta', 'チ': 'chi', 'ツ': 'tsu', 'テ': 'te', 'ト': 'to',
    'ダ': 'da', 'ヂ': 'ji', 'ヅ': 'zu', 'デ': 'de', 'ド': 'do',
    'ナ': 'na', 'ニ': 'ni', 'ヌ': 'nu', 'ネ': 'ne', 'ノ': 'no',
    'ハ': 'ha', 'ヒ': 'hi', 'フ': 'fu', 'ヘ': 'he', 'ホ': 'ho',
    'バ': 'ba', 'ビ': 'bi', 'ブ': 'bu', 'ベ': 'be', 'ボ': 'bo',
    'パ': 'pa', 'ピ': 'pi', 'プ': 'pu', 'ペ': 'pe', 'ポ': 'po',
    'マ': 'ma', 'ミ': 'mi', 'ム': 'mu', 'メ': 'me', 'モ': 'mo',
    'ヤ': 'ya', 'ユ': 'yu', 'ヨ': 'yo',
    'ラ': 'ra', 'リ': 'ri', 'ル': 'ru', 'レ': 're', 'ロ': 'ro',
    'ワ': 'wa', 'ヰ': 'i', 'ヱ': 'e', 'ヲ': 'o', 'ン': 'n', 'ヴ': 'vu',
    'ヵ': 'ka', 'ヶ': 'ke',
}
_JA_SMALL_Y = {'ャ': 'a', 'ュ': 'u', 'ョ': 'o'}
# Tổ hợp mở rộng (từ mượn): âm đầu + nguyên âm nhỏ
_JA_COMBO = {
    'ファ': 'fa', 'フィ': 'fi', 'フェ': 'fe', 'フォ': 'fo', 'フュ': 'fyu',
    'ティ': 'ti', 'トゥ': 'tu', 'ディ': 'di', 'ドゥ': 'du', 'テュ': 'tyu', 'デュ': 'dyu',
    'ウィ': 'wi', 'ウェ': 'we', 'ウォ': 'wo', 'ヴァ': 'va', 'ヴィ': 'vi', 'ヴェ': 've', 'ヴォ': 'vo',
    'シェ': 'she', 'ジェ': 'je', 'チェ': 'che', 'ツァ': 'tsa', 'ツィ': 'tsi', 'ツェ': 'tse', 'ツォ': 'tso',
    'イェ': 'ye',
}
_MACRON = {'a': 'ā', 'i': 'ī', 'u': 'ū', 'e': 'ē', 'o': 'ō'}
_JA_PUNCT = {'。': '.', '、': ',', '，': ',', '！': '!', '？': '?', '：': ':', '；': ';', '「': '“', '」': '”', '『': '“',
             '』': '”', '（': '(', '）': ')', '・': ' ', '〜': '~', '…': '...', '　': ' '}


def _double_first(romaji):
    """Gấp đôi phụ âm đầu (ッ): kitte, zasshi, matchi (tch)."""
    if not romaji or romaji[0] in 'aiueon':
        return romaji
    return ('t' + romaji) if romaji.startswith('ch') else romaji[0] + romaji


def _kana_to_hepburn(kana, double_first=False):
    """Katakana (hoặc hiragana) → Hepburn, nguyên âm dài viết bằng dấu ngang (ou, oo → ō; uu → ū; aa → ā; ee → ē; ei, ii giữ nguyên)."""
    kana = ''.join(_HIRA_TO_KATA.get(c, c) for c in kana)
    out = []  # mỗi phần tử là một âm tiết latin
    double_next = double_first
    i = 0
    while i < len(kana):
        c = kana[i]
        pair = kana[i:i + 2]
        if c == 'ッ':
            double_next = True
            i += 1
            continue
        if c == 'ー':
            if out and out[-1] and out[-1][-1] in _MACRON:
                out[-1] = out[-1][:-1] + _MACRON[out[-1][-1]]
            i += 1
            continue
        if pair in _JA_COMBO:
            syl, step = _JA_COMBO[pair], 2
        elif c in _JA_BASE:
            syl, step = _JA_BASE[c], 1
            nxt = kana[i + 1] if i + 1 < len(kana) else ''
            if nxt in _JA_SMALL_Y and syl.endswith('i') and c not in 'イウ':
                stem = syl[:-1]
                vowel = _JA_SMALL_Y[nxt]
                if syl in ('shi', 'chi', 'ji'):
                    syl = stem + vowel  # シャ → sha, チャ → cha, ジャ → ja
                else:
                    syl = stem + 'y' + vowel
                step = 2
        else:
            out.append(c)
            i += 1
            double_next = False
            continue
        if double_next:
            syl = ('t' + syl) if syl.startswith('ch') else (syl[0] + syl)
            double_next = False
        if syl == 'n':
            nxt = kana[i + 1:i + 2]
            if nxt and (nxt in 'アイウエオヤユヨ' or nxt in _JA_BASE and _JA_BASE[nxt][0] in 'aiueoy' and nxt not in 'ンッ'):
                syl = "n'"
        out.append(syl)
        i += step
    text = ''.join(out)
    return re.sub(r'ou|oo|uu|aa|ee', lambda m: _MACRON[m.group(0)[0]], text)


def _ja_tokens(text):
    from utils.utils.kanji import get_ruby_generator
    return get_ruby_generator().tagger(text)


def _ja_token_kana(word):
    """Cách đọc katakana của token; trợ từ は へ を đọc wa e o."""
    feature = word.feature
    kana = getattr(feature, 'kana', None)
    if not kana or kana == '*':
        return None
    pron = getattr(feature, 'pron', None)
    if pron and pron != '*' and len(pron) == len(kana):
        kana = ''.join(p if k in 'ハヘヲ' and p in 'ワエオ' else k for k, p in zip(kana, pron))
    return kana


_JA_SEPARATE_AUX = ('だ', 'です')  # trợ động từ viết rời (inu da, gakusei desu); các trợ động từ khác dính vào từ trước


def romanize_ja(text, as_word=False):
    """
    Hepburn cho cả câu. Trợ từ viết rời (watakushi wa neko o ...), trợ động từ / hậu tố dính vào từ trước (trừ だ / です),
    tiền tố dính vào từ sau (o-hahasan). as_word=True: viết liền hết (cho từ đơn).
    """
    if not text:
        return ''
    out = ''
    pending_double = False
    glue_next = False
    for word in _ja_tokens(text):
        surface = word.surface
        feature = word.feature
        pos = getattr(feature, 'pos1', '') or ''
        kana = _ja_token_kana(word)
        sokuon_end = False
        if kana:
            if kana.endswith('ッ'):
                kana, sokuon_end = kana[:-1], True
            piece = _kana_to_hepburn(kana, double_first=pending_double)
        elif surface and all(KANA.match(c) or c == 'ー' for c in surface):
            kana = surface
            if kana.endswith(('ッ', 'っ')):
                kana, sokuon_end = kana[:-1], True
            piece = _kana_to_hepburn(kana, double_first=pending_double)
        elif pos == '補助記号' or all(c in _JA_PUNCT for c in surface):
            piece = ''.join(_JA_PUNCT.get(c, c) for c in surface)
            if piece.strip() in ('.', ',', '!', '?', ':', ';', ')', '”', '...', '~'):
                out = out.rstrip() + piece
            else:
                out += (' ' if out and not out.endswith(' ') else '') + piece
            pending_double = False
            glue_next = piece.strip() in ('(', '“')
            continue
        else:
            piece = surface
        pending_double = sokuon_end
        lemma = (getattr(feature, 'lemma', '') or '').split('-')[0]
        separate_aux = pos == '助動詞' and lemma in _JA_SEPARATE_AUX
        glue = (as_word or not out or glue_next or out.endswith((' ', '(', '“'))
                or (pos in ('助動詞', '接尾辞') and not separate_aux) or (pos == '助詞' and surface in ('て', 'で')))
        out += piece if glue else ' ' + piece
        glue_next = pos == '接頭辞'
    return re.sub(r'\s+', ' ', out).strip()


def ja_reading_hiragana(text):
    """Cách đọc hiragana của cả chuỗi (kana giữ nguyên)."""
    parts = []
    for word in _ja_tokens(text):
        kana = _ja_token_kana(word)
        parts.append(''.join(_KATA_TO_HIRA.get(c, c) for c in kana) if kana else word.surface)
    return ''.join(parts)


# ============================== Giao diện chung ==============================

def romanize(text, language_code, as_word=False):
    """Phiên âm Latin của text; '' nếu ngôn ngữ không hỗ trợ hoặc text không có chữ của ngôn ngữ đó."""
    lang = _lang(language_code)
    if lang not in ROMAN_LANGS or not has_script(text, lang):
        return ''
    try:
        if lang == 'zh':
            return romanize_zh(text, as_word=as_word and zh_is_word(text))
        if lang == 'ko':
            return romanize_ko(text)
        return romanize_ja(text, as_word=as_word)
    except Exception as e:  # không chặn việc tạo từ
        logger.warning('Không phiên âm được %r (%s): %s', text, lang, e)
        return ''


def add_roman(contents, language_code):
    """Gắn `roman` cho definition, usage và từng example (ngôn ngữ của từ). Đoạn không có chữ của ngôn ngữ đó thì bỏ roman."""
    lang = _lang(language_code)
    if lang not in ROMAN_LANGS:
        return
    nodes = [(contents.get('definition') or {}).get(language_code), (contents.get('usage') or {}).get(language_code)]
    nodes += [ex.get(language_code) for ex in contents.get('examples') or [] if isinstance(ex, dict)]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        roman = romanize(node.get('value') or '', lang)
        if roman:
            node['roman'] = roman
        else:
            node.pop('roman', None)


def add_readings(contents, language_code):
    """Một lệnh cho mọi chỗ sinh / sửa nội dung: ruby (chỉ tiếng Nhật) + roman (ja, zh, ko). Không tốn AI."""
    lang = _lang(language_code)
    if lang == 'ja':
        add_ruby(contents, language_code)
    add_roman(contents, language_code)


def _toneless(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text or '') if not unicodedata.combining(c)).replace(' ', '').replace("'", '').lower()


def reading_ipas(word_value, language_code, ai_ipas=None):
    """
    Trường ipas cho ja / zh / ko: một mục {label: READING, value, roman}. Ngôn ngữ khác trả lại ai_ipas.
    - ja: value = hiragana (nghĩa đọc khác nhau thì giữ cách đọc AI chọn nếu đó là kana), roman = Hepburn.
    - zh: value = roman = pinyin có dấu thanh. Giữ thanh điệu AI chọn cho nghĩa này khi âm tiết khớp với pinyin của từ (行 háng / xíng).
    - ko: value = chữ Hangul, roman = Revised Romanization.
    """
    lang = _lang(language_code)
    if lang not in ROMAN_LANGS or not has_script(word_value, lang):
        return ai_ipas
    ai_value = ''
    for item in ai_ipas or []:
        if isinstance(item, dict) and (item.get('value') or '').strip():
            ai_value = item['value'].strip().strip('/').strip()
            break
    try:
        if lang == 'ja':
            if ai_value and KANA_ONLY.match(ai_value):
                value = ''.join(_KATA_TO_HIRA.get(c, c) for c in ai_value)
            else:
                value = ja_reading_hiragana(word_value)
            roman = _kana_to_hepburn(value) if KANA_ONLY.match(value) else romanize_ja(word_value, as_word=True)
        elif lang == 'zh':
            computed = romanize_zh(word_value, as_word=zh_is_word(word_value))
            value = computed
            if ai_value and ai_value.isascii() is False and _toneless(ai_value) == _toneless(computed) and re.search(r'[a-zü]', ai_value):
                value = ai_value if (' ' in ai_value) == (' ' in computed) else computed
            roman = value
        else:
            value, roman = word_value.strip(), romanize_ko(word_value.strip())
    except Exception as e:
        logger.warning('Không dựng được phiên âm cho %r (%s): %s', word_value, lang, e)
        return ai_ipas
    if not roman:
        return ai_ipas
    return [{'label': 'READING', 'value': value, 'roman': roman}]
