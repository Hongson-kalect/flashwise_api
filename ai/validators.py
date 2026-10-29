"""
Làm sạch dữ liệu client gửi lên cho nghĩa tuỳ chỉnh (create-sense / update-sense).
Chỉ giữ các khoá và kiểu đã biết, cắt độ dài: client không tự đặt được id, created_by hay khoá lạ vào DB.
"""
import re
import uuid

MAX_TEXT = 1000
MAX_LANGS = 12
MAX_EXAMPLES = 5
MAX_DELTA_CHARS = 20000

LANG_RE = re.compile(r'^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})?$')

# Các trường client được phép đặt trên AISenseMetadata
METADATA_FIELDS = ('image_link', 'advanced', 'tags', 'image_keywords', 'image_metadata')


def is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def is_lang(value):
    return isinstance(value, str) and bool(LANG_RE.match(value))


def _clean_node(node):
    if not isinstance(node, dict):
        return None
    value = node.get('value')
    if not isinstance(value, str) or not value.strip():
        return None
    clean = {'value': value.strip()[:MAX_TEXT]}
    for extra in ('ruby', 'roman'):
        if isinstance(node.get(extra), str):
            clean[extra] = node[extra][:MAX_TEXT]
    return clean


def _clean_by_lang(node):
    """{lang: {"value": ...}} -> chỉ giữ ngôn ngữ hợp lệ có nội dung."""
    if not isinstance(node, dict):
        return {}
    cleaned = {}
    for lang, content in list(node.items())[:MAX_LANGS]:
        content = _clean_node(content)
        if is_lang(lang) and content:
            cleaned[lang] = content
    return cleaned


def clean_contents(contents):
    """contents của nghĩa: definition, usage (mỗi cái theo ngôn ngữ), examples (danh sách), translations (ngôn ngữ -> danh sách từ)."""
    if not isinstance(contents, dict):
        return {}
    out = {}
    for key in ('definition', 'usage'):
        langs = _clean_by_lang(contents.get(key))
        if langs:
            out[key] = langs

    if isinstance(contents.get('examples'), list):
        examples = [langs for langs in (_clean_by_lang(ex) for ex in contents['examples'][:MAX_EXAMPLES]) if langs]
        if examples:
            out['examples'] = examples

    if isinstance(contents.get('translations'), dict):
        translations = {}
        for lang, words in list(contents['translations'].items())[:MAX_LANGS]:
            if is_lang(lang) and isinstance(words, list):
                words = [w.strip()[:100] for w in words[:10] if isinstance(w, str) and w.strip()]
                if words:
                    translations[lang] = words
        if translations:
            out['translations'] = translations
    return out


def clean_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    out = {}
    link = metadata.get('image_link')
    if isinstance(link, str) and link.startswith(('http://', 'https://')):
        out['image_link'] = link[:1000]
    if isinstance(metadata.get('advanced'), dict):
        out['advanced'] = metadata['advanced']
    for key, limit in (('tags', 50), ('image_keywords', 100)):
        if isinstance(metadata.get(key), list):
            out[key] = [t.strip()[:limit] for t in metadata[key][:20] if isinstance(t, str) and t.strip()]
    if isinstance(metadata.get('image_metadata'), dict):
        out['image_metadata'] = metadata['image_metadata']
    return out
