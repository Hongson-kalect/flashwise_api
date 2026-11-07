"""Lấy chữ từ nghĩa để so khớp: định nghĩa tiếng Anh và gloss (các từ tiếng Anh dịch nghĩa)."""


def _value(x):
    if isinstance(x, dict):
        x = x.get('value')
    return x.strip() if isinstance(x, str) else ''


def en_definition(sense):
    return _value(((sense.contents or {}).get('definition') or {}).get('en'))


def gloss_words(sense, limit=3):
    """Các bản dịch tiếng Anh của nghĩa (translations.en), chỉ giữ cụm ngắn dùng được làm headword."""
    raw = ((sense.contents or {}).get('translations') or {}).get('en') or []
    out = []
    for g in raw:
        g = _value(g)
        if g and len(g.split()) <= 3 and g.lower() not in [o.lower() for o in out]:
            out.append(g)
    return out[:limit]


def head(sense):
    """Từ đứng đầu khi nhúng: tiếng Anh dùng chính từ, ngôn ngữ khác dùng gloss (không có thì dùng từ gốc)."""
    if (sense.language_code or '') == 'en':
        return sense.word_value or ''
    glosses = gloss_words(sense)
    return ', '.join(glosses) if glosses else (sense.word_value or '')


def embed_text(sense):
    return f'{head(sense)}: {en_definition(sense)}'


def pos_class(pos):
    p = (pos or '').lower().strip()
    for key in ('noun', 'verb', 'adj', 'adv'):
        if p.startswith(key):
            return key
    return p[:20]
