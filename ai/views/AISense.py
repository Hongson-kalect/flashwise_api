import json
import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import ExpressionWrapper, F, FloatField, Q
from django.db.models.functions import Cast
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.jobs import queue_translation
from ai.sense_edit import apply_sense_edit
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from ai.queries import visible_sense_q
from core.bundle import pick_languages
from core.models import CollectionItem
from django.utils import timezone
from interact.likes import set_like
from interact.models import UserInteraction
from ai.serializers.AISense import AISenseSerializer
from ai.validators import MAX_DELTA_CHARS, METADATA_FIELDS, clean_contents, clean_metadata, is_lang, is_uuid
from utils.utils.permissions import IsAdminOrReadOnly
from utils.utils.sense_handle import deep_merge, get_user_lang_sense
from utils.utils.soft_delete_viewset import SoftDeleteViewSet

logger = logging.getLogger(__name__)


def _bad_request(message):
    return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)


TOP_MAX = 100
TOP_CACHE_SECONDS = 600
TOP_MIN_REVIEWS = 20  # forget_rate: nghĩa ít lần chấm thì tỉ lệ dao động mạnh nên bỏ
TOP_SENSE_ORDERS = {
    'forgot': 'global_forget_count',
    'forget_rate': 'forget_rate',
    'learned': 'learner_count',
    'liked': 'like_count',
}


def top_senses(language_code, by):
    """Nghĩa chính thức (thống kê học ghi trên mọi bản cùng gốc nên chỉ lấy bản chính thức để không trùng), giá trị > 0."""
    queryset = (
        AISense.objects.filter(is_official=True, language_code=language_code)
        .exclude(is_deleted=True).exclude(is_active=False).select_related('metadata')
    )
    if by == 'forget_rate':
        total = F('global_forget_count') + F('global_remember_count')
        queryset = queryset.annotate(reviews=total).filter(reviews__gte=TOP_MIN_REVIEWS, global_forget_count__gt=0).annotate(
            forget_rate=ExpressionWrapper(Cast('global_forget_count', FloatField()) / total, output_field=FloatField()),
        )
    else:
        queryset = queryset.filter(**{TOP_SENSE_ORDERS[by] + '__gt': 0})
    return queryset.order_by('-' + TOP_SENSE_ORDERS[by], '-global_forget_count', 'id')


def top_sense_row(sense, language_code, user_language_code):
    forgot, remembered = sense.global_forget_count, sense.global_remember_count
    langs = {language_code, user_language_code} - {None}
    return {
        'id': str(sense.id), 'word_id': str(sense.word_id), 'word_value': sense.word_value,
        'pos': sense.pos, 'level': sense.level, 'ipas': sense.ipas, 'image_preview': sense.image_preview,
        'contents': pick_languages(sense.contents or {}, langs),
        'global_forget_count': forgot, 'global_remember_count': remembered,
        'forget_rate': round(forgot / (forgot + remembered), 4) if forgot + remembered else 0.0,
        'learner_count': sense.learner_count, 'like_count': sense.like_count, 'global_difficulty': sense.global_difficulty,
    }


class AISenseViewSet(SoftDeleteViewSet):
    queryset = AISense.objects.select_related('metadata', 'original')
    serializer_class = AISenseSerializer
    # CRUD trực tiếp lên sense hệ thống chỉ admin, user tạo / sửa sense custom qua create-sense / update-sense
    permission_classes = [IsAdminOrReadOnly]

    def get_permissions(self):
        if self.action == 'destroy':
            return [IsAuthenticated()]  # user xoá nghĩa của chính mình, kiểm tra chủ trong destroy
        return super().get_permissions()

    def get_queryset(self):
        """Nghĩa chính thức, nghĩa của chính mình và nghĩa người khác đã upload trong bộ đã phát hành (admin thấy hết)."""
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return queryset
        return queryset.filter(visible_sense_q(user))

    def destroy(self, request, *args, **kwargs):
        """
        Admin: xoá mềm bất kỳ nghĩa nào. User: chỉ nghĩa tuỳ chỉnh của mình chưa đóng băng (đã upload thì bộ của người khác
        đang trỏ tới nên không xoá được, dùng hide). Các dòng nháp của user trong bộ từ trỏ tới nghĩa này bị bỏ theo.
        """
        sense = self.get_object()
        user = request.user
        if not user.is_staff:
            if sense.is_official or sense.created_by_id != user.id:
                return Response({"message": "Only your own custom senses can be deleted, hide it instead"}, status=status.HTTP_403_FORBIDDEN)
            if sense.is_frozen:
                return Response({"message": "Published sense cannot be deleted, hide it instead", "errorCode": "sense_frozen"}, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            sense.is_deleted = True
            sense.deleted_at = timezone.now()
            sense.updated_by = user
            sense.save(update_fields=['is_deleted', 'deleted_at', 'updated_by', 'updated_at'])
            CollectionItem.objects.filter(sense=sense, released=False, created_by=sense.created_by).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post', 'delete'], url_path='hide', permission_classes=[IsAuthenticated])
    def hide(self, request, pk=None):
        """POST: ẩn nghĩa (thường là nghĩa chính thức) khỏi get-word của riêng mình, DELETE: bỏ ẩn. Nghĩa của mình thì xoá chứ không ẩn."""
        sense = self.get_object()
        if sense.created_by_id == request.user.id and not sense.is_official:
            return _bad_request("Delete your own sense instead of hiding it")
        want = request.method == 'POST'
        UserInteraction.objects.update_or_create(
            user=request.user, interaction_type='hide', target_type='sense', target_id=sense.id,
            defaults={'status': want, 'created_by': request.user},
        )
        return Response({'hidden': want, 'sense_id': str(sense.id)})

    @action(detail=True, methods=['post', 'delete'], url_path='like', permission_classes=[IsAuthenticated])
    def like(self, request, pk=None):
        """POST: thích (tim) nghĩa, DELETE: bỏ thích. Mỗi bản nghĩa (chính thức / tuỳ chỉnh) đếm riêng, gọi lại không đếm trùng."""
        sense = self.get_object()
        versions, like_count = set_like(request.user, AISense, 'sense', sense.id, request.method == 'POST')
        return Response({'liked': bool(versions), 'sense_id': str(sense.id), 'like_count': like_count})

    @action(detail=False, methods=['get'], url_path='top')
    def top(self, request):
        """
        Nghĩa chính thức đứng đầu toàn hệ thống theo `by`: forgot (số lần chấm Again), forget_rate (tỉ lệ quên, cần
        >= TOP_MIN_REVIEWS lần chấm), learned (số người học), liked (lượt thích). Query: lang (bắt buộc), by, limit (<= 100),
        user_lang (kèm nội dung ngôn ngữ đó, không đẩy dịch bù). Cache TOP_CACHE_SECONDS.
        """
        params = request.query_params
        language_code, user_language_code, by = params.get('lang'), params.get('user_lang'), params.get('by', 'forgot')
        if not is_lang(language_code) or (user_language_code is not None and not is_lang(user_language_code)):
            return _bad_request("lang is required, user_lang must be a language code")
        if by not in TOP_SENSE_ORDERS:
            return _bad_request("by must be one of: %s" % ', '.join(TOP_SENSE_ORDERS))
        try:
            limit = min(max(int(params.get('limit', 20)), 1), TOP_MAX)
        except ValueError:
            return _bad_request("Invalid limit")

        key = f'top_senses:{language_code}:{user_language_code or ""}:{by}'
        rows = cache.get(key)
        if rows is None:
            rows = [top_sense_row(sense, language_code, user_language_code) for sense in top_senses(language_code, by)[:TOP_MAX]]
            cache.set(key, rows, TOP_CACHE_SECONDS)
        return Response({'by': by, 'lang': language_code, 'senses': rows[:limit]})

    @action(detail=False, methods=['get'], url_path='mine', permission_classes=[IsAuthenticated])
    def mine(self, request):
        """Nghĩa tuỳ chỉnh của mình (lọc theo word_id nếu có), id các nghĩa đang ẩn và đang thích. Tối đa 500 nghĩa mới nhất."""
        word_id = request.query_params.get('word_id')
        if word_id is not None and not is_uuid(word_id):
            return _bad_request("Invalid word_id")
        senses = AISense.objects.filter(created_by=request.user, is_official=False).exclude(is_deleted=True).exclude(is_active=False)
        mine = UserInteraction.objects.filter(user=request.user, target_type='sense', status=True)
        hidden, liked = mine.filter(interaction_type='hide'), mine.filter(interaction_type='like')
        if word_id:
            senses = senses.filter(word_id=word_id)
            word_senses = AISense.objects.filter(word_id=word_id).values('id')
            hidden, liked = hidden.filter(target_id__in=word_senses), liked.filter(target_id__in=word_senses)
        senses = senses.select_related('metadata').order_by('-id')[:500]
        return Response({
            'senses': AISenseSerializer(senses, many=True).data,
            'hidden': [str(target) for target in hidden.values_list('target_id', flat=True)],
            'liked': [str(target) for target in liked.values_list('target_id', flat=True)],
        })

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
        sense = apply_sense_edit(user, sense, delta, raw_metadata, image)

        data = AISenseSerializer(sense).data
        data['contents'] = self._user_contents(sense, user_language_code)
        return Response(data, status=status.HTTP_200_OK)
