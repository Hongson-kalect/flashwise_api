from django.db import transaction

from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.validators import METADATA_FIELDS, clean_metadata
from utils.ai.japanese import add_ruby
from utils.utils.sense_handle import deep_merge


def apply_sense_edit(user, sense, delta, raw_metadata=None, image=None, audio=None):
    """
    Sửa nghĩa bằng delta, trả về nghĩa kết quả. Nghĩa chính thức, đã đóng băng hoặc của người khác không bị sửa tại chỗ
    mà tách thành một bản tuỳ chỉnh mới của user; chỉ nghĩa tuỳ chỉnh của chính user (chưa đóng băng) mới sửa tại chỗ.
    Dùng chung cho ai-senses/update-sense và user-collections/update-sense.
    """
    if image is None:
        image = sense.image_preview
    if audio is None:
        audio = sense.audio  # None = giữ phát âm cũ, '' = bỏ (dùng audio của từ / giọng máy)
    if sense.language_code == 'ja':
        # Chữ Nhật vừa sửa không còn khớp furigana cũ: sinh lại ruby bằng fugashi (không tốn AI). Roman chỉ có khi client gửi kèm
        add_ruby(delta, 'ja')
    in_place = not sense.is_official and not sense.is_frozen and (user.is_staff or sense.created_by_id == user.id)

    with transaction.atomic():
        metadata_instance = sense.metadata
        if isinstance(raw_metadata, dict) and raw_metadata:
            # Metadata là dữ liệu dùng chung nên không sửa tại chỗ: tạo bản mới từ bản cũ cộng phần user gửi
            base = {f: getattr(sense.metadata, f) for f in METADATA_FIELDS} if sense.metadata else {}
            metadata_instance = AISenseMetadata.objects.create(**{**base, **clean_metadata(raw_metadata)}, created_by=user)

        if in_place:
            sense.delta = deep_merge(delta, sense.delta)
            sense.image_preview = image
            sense.audio = audio or None
            sense.metadata = metadata_instance
            sense.updated_by = user
            sense.save()
            return sense
        return AISense.objects.create(
            word_id=sense.word_id, word_value=sense.word_value, delta=delta, image_preview=image, audio=audio or None,
            metadata=metadata_instance, created_by=user, language_code=sense.language_code,
            pos=sense.pos, level=sense.level, register=sense.register, ipas=sense.ipas, is_offensive=sense.is_offensive,
            is_official=False, is_frozen=False, is_ai_created=False,
            original=sense.original or sense, previous=None if sense.is_official else sense,
        )
