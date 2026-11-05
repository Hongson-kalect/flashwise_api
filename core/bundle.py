"""
Gói học offline của 1 bộ từ (GET user-collections/{id}/bundle): mọi thứ app cần để học không cần mạng,
mỗi bảng là danh sách dòng đúng tên cột SQLite phía app (flashwise/database/schema).

- Dòng bộ từ: bản user đang thấy (live_items: nháp của chính user thắng bản phát hành tới version user dùng).
- Nội dung nghĩa chỉ giữ ngôn ngữ gốc + ngôn ngữ của user. Nghĩa thiếu bản dịch: id trong `pending`, đếm ở
  user_collection.translation_missing. Tải bộ KHÔNG đẩy việc dịch (dịch lười: app gọi ai-senses/translate cho cụm sắp học,
  hoặc get-word / learn/* đẩy khi chạm tới); dịch xong nghĩa đổi updated_at nên etag đổi, app tải lại.
- Nghĩa bị ban (ai/queries.banned_sense_ids) bị bỏ khỏi gói cùng dòng bộ từ trỏ tới nó, id nằm trong `banned`
  để app xoá bản đã lưu. Bộ bị ban thì view trả 403 collection_banned, không có gói.
- etag: băm các mốc cập nhật của mọi thứ trong gói; app gửi lại etag cũ, không đổi thì chỉ nhận {unchanged: true}.
"""
import hashlib
import json

from ai.models import AISense
from ai.queries import banned_sense_ids
from ai.translation import collect_gaps, missing_sense_ids, queue_gaps
from core.queries import current_items

ITEM_COLUMNS = ('id', 'value', 'collection_id', 'original_id', 'sense_id', 'display_order', 'status', 'version',
                'released', 'is_base', 'is_active', 'is_deleted', 'parent', 'created_by_id', 'created_at', 'updated_at')
WORD_COLUMNS = ('id', 'value', 'language_code', 'status', 'description', 'ipas', 'audio', 'is_active', 'created_at', 'updated_at')
SENSE_COLUMNS = ('id', 'word_id', 'word_value', 'original_id', 'metadata_id', 'image_context_id', 'language_code', 'preview',
                 'image_preview', 'contents', 'delta', 'ipas', 'audio', 'pos', 'level', 'register', 'is_official',
                 'is_offensive', 'score', 'like_count', 'main_count', 'global_difficulty', 'learner_count',
                 'is_deleted', 'created_at', 'updated_at')
METADATA_COLUMNS = ('id', 'image_link', 'advanced', 'tags', 'image_keywords', 'image_metadata', 'is_active', 'created_at', 'updated_at')
COLLECTION_COLUMNS = ('id', 'name', 'description', 'image_url', 'score', 'item_count', 'language_code', 'downloaded_count',
                      'like_count', 'view_count', 'remix_count', 'metadatas', 'parents', 'previous_id', 'original_id', 'invalid_words', 'pending_words',
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
    # Gồm cả dòng xoá (bia mộ nháp của chính user) để app đếm được thay đổi chưa phát hành; app bỏ dòng is_deleted khi hiển thị.
    items = sorted(
        current_items(collection.id, user_collection.version, user),
        key=lambda row: (row.display_order or 0, row.created_at),
    )
    banned = banned_sense_ids({item.sense_id for item in items})
    items = [item for item in items if str(item.sense_id) not in banned]
    senses = list(
        AISense.objects.filter(id__in=[item.sense_id for item in items])
        .select_related('word', 'metadata', 'original').order_by('id')
    )
    return collection, items, senses, sorted(banned)


def build_bundle(user_collection, user, user_language_code, collection, items, senses, banned=()):
    tables = sense_tables(senses, user_language_code, queue=False)
    live = {str(item.sense_id) for item in items if not item.is_deleted}
    return {
        # is_author / is_released: app biết có cho đổi ảnh bìa không (chỉ tác giả, bộ chưa phát hành); chỉ lưu ở máy.
        # translation_missing: số nghĩa trong bộ chưa có bản dịch cho ngôn ngữ người xem (app hiện "đã dịch x / n")
        'user_collection': {
            **row(user_collection, USER_COLLECTION_COLUMNS),
            'is_author': collection.created_by_id == user.id,
            'is_released': bool(collection.released),
            'translation_missing': sum(1 for sense_id in tables['pending'] if sense_id in live),
        },
        'collection': row(collection, COLLECTION_COLUMNS),
        'items': [row(item, ITEM_COLUMNS) for item in items],
        **tables,
        'banned': list(banned),
    }


def sense_tables(senses, user_language_code, queue=True):
    """
    Các bảng ai_word / ai_sense / sense_metadata cho danh sách nghĩa (đã select_related word, metadata, original),
    nội dung chỉ giữ ngôn ngữ gốc + user. Nghĩa thiếu bản dịch: id nằm trong `pending`; queue=True thì đẩy việc dịch
    (ai/translation.py), tải bộ dùng queue=False để không bắn cả bộ vào hàng đợi.
    """
    langs = {user_language_code} | {s.language_code for s in senses}
    words, metadata, sense_rows = {}, {}, []

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

    gaps = collect_gaps(senses, user_language_code)
    if queue:
        queue_gaps(gaps, user_language_code)
    pending = missing_sense_ids(gaps)

    return {
        'words': list(words.values()),
        'senses': sense_rows,
        'sense_metadata': list(metadata.values()),
        'pending': pending,
    }
