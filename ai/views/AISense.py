import json
import logging

from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.jobs import queue_translation
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from ai.serializers.AISense import AISenseSerializer
from ai.validators import MAX_DELTA_CHARS, METADATA_FIELDS, clean_contents, clean_metadata, is_lang, is_uuid
from utils.utils.permissions import IsAdminOrReadOnly
from utils.utils.sense_handle import deep_merge, get_user_lang_sense
from utils.utils.soft_delete_viewset import SoftDeleteViewSet

logger = logging.getLogger(__name__)


def _bad_request(message):
    return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)


class AISenseViewSet(SoftDeleteViewSet):
    queryset = AISense.objects.select_related('metadata', 'original')
    serializer_class = AISenseSerializer
    # CRUD trực tiếp lên sense hệ thống chỉ admin, user tạo / sửa sense custom qua create-sense / update-sense
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        """Người dùng chỉ thấy nghĩa chính thức và nghĩa tuỳ chỉnh do chính mình tạo (admin thấy hết)."""
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return queryset
        visible = Q(is_official=True)
        if user.is_authenticated:
            visible |= Q(created_by=user)
        return queryset.filter(visible)

    def _user_contents(self, sense, user_language_code):
        """
        Nội dung nghĩa theo ngôn ngữ của user; thiếu bản dịch thì đẩy job dịch bù cho worker.
        Nghĩa tuỳ chỉnh dạng delta không có contents riêng mà dùng contents của nghĩa gốc, nên bản dịch bù
        cũng phải ghi vào nghĩa gốc (job trỏ tới nghĩa gốc).
        """
        target = sense if sense.contents else sense.original
        if target is None:
            return {}
        contents, missing = get_user_lang_sense(sense.language_code, user_language_code, target.contents, str(target.id))
        if missing:
            queue_translation(sense.word_id, sense.language_code, user_language_code, [missing], [AISenseSerializer(target).data])
        return contents

    def retrieve(self, request, *args, **kwargs):
        sense = self.get_object()
        user_language_code = request.query_params.get('user_lang', 'en')
        if not is_lang(user_language_code):
            return _bad_request("Invalid user_lang")

        data = AISenseSerializer(sense).data
        data['contents'] = self._user_contents(sense, user_language_code)
        return Response(data)

    @action(detail=False, methods=['post'], url_path='create-sense', permission_classes=[IsAuthenticated])
    def create_sense(self, request, *args, **kwargs):
        # params: word_id, user_lang, contents{definition{<lang>{value}}, usage{}, examples[{}], translations{}}, metadata
        user = request.user
        word_id = request.data.get('word_id')
        user_language_code = request.data.get('user_lang')
        raw_metadata = request.data.get('metadata')

        if not word_id or not user_language_code or not request.data.get('contents') or not isinstance(raw_metadata, dict):
            return _bad_request("Missing required parameters")
        if not is_uuid(word_id) or not is_lang(user_language_code):
            return _bad_request("Invalid word_id or user_lang")

        word_instance = AIWord.objects.filter(id=word_id).exclude(is_deleted=True).first()
        if not word_instance:
            return Response({"message": "Word not found"}, status=status.HTTP_404_NOT_FOUND)

        contents = clean_contents(request.data.get('contents'))
        if not contents.get('definition', {}).get(word_instance.language_code):
            return _bad_request("Missing definition")

        with transaction.atomic():
            metadata = AISenseMetadata.objects.create(**clean_metadata(raw_metadata), created_by=user)
            sense_instance = AISense.objects.create(
                word=word_instance, word_value=word_instance.value, metadata=metadata,
                is_frozen=None, contents=contents, created_by=user,
                language_code=word_instance.language_code, is_official=False, is_ai_created=False,
            )

        data = AISenseSerializer(sense_instance).data
        _, missing = get_user_lang_sense(word_instance.language_code, user_language_code, contents, str(sense_instance.id))
        if missing:
            # Phần còn lại dịch xong sẽ về qua socket / lần tra sau
            queue_translation(word_instance.id, word_instance.language_code, user_language_code, [missing], [data])
            return Response({'detail': 'Word incomplete.', 'status': '206', 'data': data}, status=status.HTTP_206_PARTIAL_CONTENT)

        return Response({'detail': 'Sense created.', 'status': '201', 'data': data}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['put'], url_path='update-sense', permission_classes=[IsAuthenticated])
    def update_sense(self, request, *args, **kwargs):
        """
        Sửa nghĩa bằng delta. Nghĩa chính thức, nghĩa đã đóng băng hoặc nghĩa của người khác không bị sửa tại chỗ
        mà tách ra thành một bản tuỳ chỉnh mới của user; chỉ nghĩa tuỳ chỉnh của chính user (chưa đóng băng) mới sửa tại chỗ.
        """
        user = request.user
        sense_id = request.data.get('sense_id')
        user_language_code = request.data.get('user_lang')
        delta = request.data.get('delta') or {}
        raw_metadata = request.data.get('metadata')

        if not is_uuid(sense_id) or not is_lang(user_language_code):
            return _bad_request("Invalid sense_id or user_lang")
        if not isinstance(delta, dict) or len(json.dumps(delta)) > MAX_DELTA_CHARS:
            return _bad_request("Invalid delta")
        image = request.data.get('image')
        if image is not None and (not isinstance(image, str) or len(image) > 1000):
            return _bad_request("Invalid image")

        sense = self.get_queryset().filter(id=sense_id).first()
        if not sense:
            return Response({"message": "Sense not found"}, status=status.HTTP_404_NOT_FOUND)
        if image is None:
            image = sense.image_preview

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
                sense.metadata = metadata_instance
                sense.updated_by = user
                sense.save()
            else:
                sense = AISense.objects.create(
                    word_id=sense.word_id, word_value=sense.word_value, delta=delta, image_preview=image,
                    metadata=metadata_instance, created_by=user, language_code=sense.language_code,
                    is_official=False, is_frozen=False, is_ai_created=False,
                    original=sense.original or sense, previous=None if sense.is_official else sense,
                )

        data = AISenseSerializer(sense).data
        data['contents'] = self._user_contents(sense, user_language_code)
        return Response(data, status=status.HTTP_200_OK)
