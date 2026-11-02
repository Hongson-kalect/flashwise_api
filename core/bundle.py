"""
Gói học offline của 1 bộ từ (GET user-collections/{id}/bundle): mọi thứ app cần để học không cần mạng,
mỗi bảng là danh sách dòng đúng tên cột SQLite phía app (flashwise/database/schema).

- Dòng bộ từ: bản user đang thấy (live_items: nháp của chính user thắng bản phát hành tới version user dùng).
- Nội dung nghĩa chỉ giữ ngôn ngữ gốc + ngôn ngữ của user. Thiếu bản dịch thì đẩy hàng dịch, trả id trong `pending`
  (dịch xong nghĩa đổi updated_at nên etag đổi, app tải lại).
- Nghĩa bị ban (ai/queries.banned_sense_ids) bị bỏ khỏi gói cùng dòng bộ từ trỏ tới nó, id nằm trong `banned`
  để app xoá bản đã lưu. Bộ bị ban thì view trả 403 collection_banned, không có gói.
- etag: băm các mốc cập nhật của mọi thứ trong gói; app gửi lại etag cũ, không đổi thì chỉ nhận {unchanged: true}.
"""
import hashlib
import json
from collections import defaultdict

from ai.jobs import queue_translation
from ai.models import AISense
from ai.queries import banned_sense_ids
from ai.serializers.AISense import AISenseSerializer
from core.queries import live_items
from utils.utils.sense_handle import get_user_lang_sense

ITEM_COLUMNS = ('id', 'value', 'collection_id', 'original_id', 'sense_id', 'display_order', 'status', 'version',
                'released', 'is_base', 'is_active', 'is_deleted', 'parent', 'created_by_id', 'created_at', 'updated_at')
WORD_COLUMNS = ('id', 'value', 'language_code', 'status', 'description', 'ipas', 'audio', 'is_active', 'created_at', 'updated_at')
SENSE_COLUMNS = ('id', 'word_id', 'word_value', 'original_id', 'metadata_id', 'image_context_id', 'language_code', 'preview',
                 'image_preview', 'contents', 'delta', 'ipas', 'audio', 'pos', 'level', 'register', 'is_official',
                 'is_offensive', 'score', 'like_count', 'main_count', 'global_difficulty', 'learner_count',
                 'is_deleted', 'created_at', 'updated_at')
METADATA_COLUMNS = ('id', 'image_link', 'advanced', 'tags', 'image_keywords', 'image_metadata', 'is_active', 'created_at', 'updated_at')
COLLECTION_COLUMNS = ('id', 'name', 'description', 'image_url', 'score', 'item_count', 'language_code', 'downloaded_count',
                      'like_count', 'view_count', 'metadatas', 'parents', 'previous_id', 'invalid_words', 'pending_words',
                      'version', 'is_official', 'is_active', 'is_frozen', 'is_deleted', 'created_at', 'updated_at')
USER_COLLECTION_COLUMNS = ('id', 'collection_id', 'name', 'description', 'learned_count', 'version', 'is_active',
                           'is_deleted', 'created_at', 'updated_at')

# Tên cột phía app khác tên thuộc tính Django
RENAME = {'created_by_id': 'created_by', 'previous_id': 'previous'}


def row(obj, columns):
    return {RENAME.get(name, name): getattr(obj, name) for name in columns}


def pick_languages(contents, langs):
    """Giữ các ngôn ngữ trong langs ở mọi khoá dạng {lang: ...} (definition, usage, translations, pro_tip...) và từng example."""
    if not isinstance(contents, dict):
        return contents

    def keep(node):
        return {lang: value for lang, value in node.items() if lang in langs} if isinstance(node, dict) else node

    return {key: [keep(item) for item in value] if isinstance(value, list) else keep(value) for key, value in contents.items()}


def _stamp(value):
    return value.isoformat() if value else ''


def make_etag(user_collection, collection, items, senses, user_language_code, banned=()):
    parts = [list(banned), 
        user_language_code, user_collection.version, _stamp(user_collection.updated_at), _stamp(collection.updated_at),
        [(str(i.id), str(i.sense_id), i.display_order, _stamp(i.updated_at)) for i in items],
        [(str(s.id), _stamp(s.updated_at), _stamp(s.word.updated_at), _stamp(s.metadata.updated_at) if s.metadata else '') for s in senses],
    ]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()


def load_bundle(user_collection, user):
    """
    Dòng bộ từ đang hiệu lực + nghĩa của chúng (kèm từ, metadata, bản gốc để lấy nội dung cho nghĩa dạng delta).
    Trả (collection, items, senses, banned): dòng / nghĩa bị ban đã bị bỏ, banned = id các nghĩa đó.
    """
    collection = user_collection.collection
    items = live_items(collection.id, user_collection.version, user)
    banned = banned_sense_ids({item.sense_id for item in items})
    items = [item for item in items if str(item.sense_id) not in banned]
    senses = list(
        AISense.objects.filter(id__in=[item.sense_id for item in items])
        .select_related('word', 'metadata', 'original').order_by('id')
    )
    return collection, items, senses, sorted(banned)


def build_bundle(user_collection, user, user_language_code, collection, items, senses, banned=()):
    return {
        'user_collection': row(user_collection, USER_COLLECTION_COLUMNS),
        'collection': row(collection, COLLECTION_COLUMNS),
        'items': [row(item, ITEM_COLUMNS) for item in items],
        **sense_tables(senses, user_language_code),
        'banned': list(banned),
    }


def sense_tables(senses, user_language_code):
    """
    Các bảng ai_word / ai_sense / sense_metadata cho danh sách nghĩa (đã select_related word, metadata, original),
    nội dung chỉ giữ ngôn ngữ gốc + user. Nghĩa thiếu bản dịch: đẩy hàng dịch theo từ, id nằm trong `pending`.
    """
    langs = {user_language_code} | {s.language_code for s in senses}
    words, metadata, sense_rows = {}, {}, []
    missing_by_word = defaultdict(list)

    for sense in senses:
        data = row(sense, SENSE_COLUMNS)
        contents = sense.contents
        if not contents and sense.original_id:
            # Nghĩa riêng dạng delta: nội dung của bản gốc, app áp delta lên (như get-word)
            contents = sense.original.contents
        data['contents'] = pick_languages(contents or {}, langs)
        sense_rows.append(data)

        words.setdefault(sense.word_id, row(sense.word, WORD_COLUMNS))
        if sense.metadata_id:
            metadata.setdefault(sense.metadata_id, row(sense.metadata, METADATA_COLUMNS))

        # Bản dịch thiếu được bù vào nghĩa đang giữ nội dung (bản gốc nếu là nghĩa delta)
        target = sense if sense.contents else sense.original
        if target is not None and target.contents and user_language_code != target.language_code:
            _, missing = get_user_lang_sense(target.language_code, user_language_code, target.contents, str(target.id))
            if missing:
                missing_by_word[target.word_id].append((target, missing))

    pending = []
    for word_id, rows in missing_by_word.items():
        rows = list({str(s.id): (s, m) for s, m in rows}.values())  # nhiều nghĩa delta cùng 1 bản gốc
        queue_translation(word_id, rows[0][0].language_code, user_language_code, [m for _, m in rows], [AISenseSerializer(s).data for s, _ in rows])
        pending.extend(str(s.id) for s, _ in rows)

    return {
        'words': list(words.values()),
        'senses': sense_rows,
        'sense_metadata': list(metadata.values()),
        'pending': pending,
    }
