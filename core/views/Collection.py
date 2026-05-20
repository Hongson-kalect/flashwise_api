from django.db.models import Prefetch, Count
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from rest_framework.decorators import action
from core.models import Collection, UserCollection
from ai.models import AISense, AIWord
from core.models.CollectionItem import CollectionItem
from core.models.UserCollection import UserCollection
from core.serializers.Collection import CollectionSerializer,CollectionPreviewSerializer,CollectionListSerializer,CollectionDetailSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.sense_handle import get_user_lang_sense
from utils.utils.uuidv7 import generate_uuid7
from django.db import transaction
from django.contrib.auth.models import User
from utils.redis.word_init import WordCacheManager
import json
import redis
                
class CollectionViewSet(SoftDeleteViewSet):
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer
    # permission_classes = [IsAuthenticatedOrReadOnly]
    permission_classes = [AllowAny] # testing

    # override create request
    # hàm này create dựa vào các sense_id có sẵn
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        senses = instance.senses.all()
        language_code = request.query_params.get('lang', 'en')
        user_language_code = request.query_params.get('user_lang', 'en')
        bulk_item = []
        missings=[]
        all_senses = []


        # Kiểm tra các từ bên trong hàng đợi, lấy danh sách các sense bên trong AIsense, cho nó vào hợp lệ hoặc không hợp lệ sựa vào tình trạng cảu sense đó ở trong bảng
        pending_words = instance.pending_words
        pending_set = set(pending_words)
        if not pending_words:
            detect_missing(language_code, user_language_code, senses)

            data = CollectionDetailSerializer(instance).data
            return Response(data, status=status.HTTP_200_OK)
        
        invalid_words = instance.invalid_words or []
        success_words = []

        words = AIWord.objects.filter(value__in=pending_words)

        if not words:
            detect_missing(language_code, user_language_code, senses)
            data = CollectionDetailSerializer(instance).data
            return Response(data, status=status.HTTP_200_OK)

        for word in words:
        # từ đang pending hoặc chưa khởi tạo được thì vẫn kệ nó
            if word.status=='INVALID':
                invalid_words.append(word.value)
                pending_set.remove(word.value)
            
            if word.status =='COMPLETED':
                success_words.append(word.value)
                pending_set.remove(word.value)
        
        if success_words:
            sense_instances = AISense.objects.filter(word_value__in=success_words).order_by('word_value','-score').distinct('word_value')
            for sense in sense_instances:
                bulk_item.append(CollectionItem(sense_id=sense.id, collection_id=instance.id, original_id=sense.original_id, value=sense.word_value))

        with transaction.atomic():
            if bulk_item:
                CollectionItem.objects.bulk_create(bulk_item)
            instance.pending_words = list(pending_set)
            instance.invalid_words = invalid_words
            instance.save()
            
            senses = instance.senses.all()

        detect_missing(language_code, user_language_code, senses)
        data = CollectionDetailSerializer(instance).data
        return Response(data, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        # params: name, description, image, tags, language_code, senses
        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]
        collection_id = generate_uuid7()

        data = request.data
        senses_data = data.pop('senses', []) # Giả sử nhận một list sense IDs {id, o_id}
        data['user_id'] = user.id
        data['id'] = collection_id

        # Sử dụng atomic để đảm bảo tính toàn vẹn dữ liệu
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        items_to_create = []
        for idx, sense_obj in enumerate(senses_data):
            items_to_create.append(
                CollectionItem(
                    collection_id=collection_id,
                    sense_id=sense_obj.get('id'), # Truyền ID trực tiếp để tránh query thêm
                    original_id=sense_obj.get('o_id'), # Truyền ID trực tiếp để tránh query thêm
                    order=idx # Gán order theo thứ tự trong list gửi
                )
            )

        with transaction.atomic():
            # 1. Lưu Collection trước
            collection = serializer.save()
            UserCollection(collection=collection, user_id=user.id).save()

            # 2. Chuẩn bị dữ liệu cho bảng phụ (CollectionItem)

            # 3. Dùng bulk_create để "bắn" toàn bộ item vào DB trong 1 câu Query
            if items_to_create:
                CollectionItem.objects.bulk_create(items_to_create)

            # UserCollection.objects.create(
            #     user=user,
            #     name=data["name"],
            #     description=data["description"],
            #     collection=collection,
            #     created_by=user,
            # )

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    #create collection by list of sense, regardless it exist or not
    @action(detail=False, methods=['post'], url_path='bulk-create')
    def create_bulk(self, request, *args, **kwargs):
        # Kết nối tới DB 0 (Làn đường xử lý)
        r_queue = redis.Redis(host='redis', port=6379, db=0)

        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]
        data = request.data
        word_values = [normalize(word) for word in data.pop('words', [])] # Giả sử nhận một list sense IDs {id, o_id}
        name = data['name']
        description = data['description']
        language_code = data['lang']
        user_language_code = data['user_lang']
        
        # Khởi tạo 1 usercollection.
        id = generate_uuid7()
        collection = Collection(id =id, name=name, description=description, language_code=language_code, is_official=False)


        # 1 lấy tất cả các sense hợp lệ đầu tiên của word với score cao nhất
        sense_instances = AISense.objects.filter(word_value__in=word_values).order_by('word_value','-score').distinct('word_value')
        sense_value_map = {sense.word_value: sense for sense in sense_instances}

        bulk_item = []
        for sense in sense_instances:
            bulk_item.append(CollectionItem(sense_id=sense.id, collection_id=id, original_id=sense.original_id, value=sense.word_value))

        # Kiểm tra những từ không có sense nào
        not_founds = set()
        invalid_words = []
        for word in word_values:
            if word not in sense_value_map:
                not_founds.add(word)

        if not_founds:
            not_found_words = AIWord.objects.filter(value__in=not_founds)
            for not_found_word in not_found_words:
                # Hoặc đã bị từ chối, hoặc đang được render chỉ giữ lại các từ cần render mới
                not_founds.remove(not_found_word.value)
                if not_found_word.status == "invalid":
                    invalid_words.append(not_found_word.value)

            collection.invalid_words = invalid_words
            collection.pending_words = list(not_founds)
            with transaction.atomic():
                collection.save()
                UserCollection.objects.create(
                    user=user,
                    collection=collection,
                    created_by=user,
                )
                CollectionItem.objects.bulk_create(bulk_item)

            # Tạo từ cho danh sách từ mới chưa có dữ liệu
            for word in not_founds:
                cache_manager = WordCacheManager()
                created, init_data = cache_manager.cache_word_init(language_code, word, user_language_code)

                # Nếu có người khác khởi tạo trước, trả về và chờ
                if not created:
                    print(f"Word '{word}' is processing")
                    cache_manager.cache_word_add_translate(language_code, word, user_language_code)
                    # return Response({'detail': 'PROCESSING', 'status': '202', 'data':init_data}, status=status.HTTP_202_ACCEPTED)

                word_instance = AIWord.objects.create(
                **init_data.get('word'),
                created_by=user,
                status= "PROCESSING"
                )

                # Đẩy vào queue "redis_word"
                r_queue.rpush("redis_word", json.dumps({
                    "user_id": str(user.id),
                    "word_id": str(word_instance.id),
                    "value": word,
                    "language_code": language_code,
                    "user_language_code": user_language_code
                }))
                # r_queue.rpush("redis_word", json.dumps(raw_word))
                
                print(f"[QUEUE] Pushed word '{word}' to redis_word queue")

        serializer = CollectionPreviewSerializer
        data = serializer(collection).data
        return Response({"data":data}, status=status.HTTP_201_CREATED)

        # Thêm tất cả các sense hợp lệ vào bảng collection item với usercollection đã tạo
        # 2. Kiểm tra có những từ nào chưa tồn tại, khởi tạo chúng và gán sense đầu tiên được khởi tạo với user collection
        # Khi này có 3 trạng thái từ: Từ ok, từ không hợp lệ, từ đang load.
        # collection item sẽ có thêm status (loading, ok, invalid, error) những từ được render sẽ có trạng thái là loading
        # collection sẽ có thêm trường value để lưu lại từ đang được kết nối sense
        # trả về collection cho user. có error và loading list. Nếu user query lại collection thì sẽ tự detect các từ có trạng thái loading và kiểm tra xem có từ tương ứng hay không. Nếu có thì trả về.
        # sau đó tiến hành render các từ chưa tồn tại. copy view AIWord: Kiểm tra cache, render các thứ...

    def get_serializer_class(self):
        # Chọn Serializer tương ứng với hành động để tối ưu dữ liệu trả về
        if self.action == 'retrieve':
            return CollectionPreviewSerializer
        return CollectionListSerializer # Mặc định cho list và các hàm khác

    def get_queryset(self):
        base_queryset = Collection.objects.filter(is_active=True)

        # KỊCH BẢN 1: USER GỌI LIST API
        if self.action == 'list':
            # Chỉ SELECT đúng bảng Collection + COUNT bảng trung gian bằng SQL. 
            # Tuyệt đối không JOIN, không lôi thông tin chi tiết của Sense lên RAM.
            return base_queryset.annotate(senses_count=Count('collectionitem'))

        # KỊCH BẢN 2: USER GỌI RETRIEVE API (XEM CHI TIẾT 1 COLLECTION)
        if self.action == 'retrieve':
            # Lúc này mới thực hiện prefetch để gom toàn bộ Sense kèm theo trong 2 câu lệnh SQL
            return base_queryset.prefetch_related(
                Prefetch(
                    'collectionitem_set',
                    queryset=CollectionItem.objects.select_related('sense').order_by('order')
                )
            )

        return base_queryset
    # Hàm list lúc này cực kỳ sạch sẽ, không cần xử lý thêm logic đếm phức tạp
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    # def retrieve(self, request, *args, **kwargs):
    #     instance = self.get_object()
    #     serializer = self.get_serializer(instance)
    #     user_language_code = request.query_params.get('user_lang', 'en')
        
    #     # Nếu muốn trả về format tùy chỉnh kèm danh sách senses
    #     data = serializer.data
    #     items = instance.collectionitem_set.all()
        
    #     # Format lại danh sách sense để client dễ dùng
    #     data['senses'] = [
    #         {
    #             "id": item.sense.id,
    #             "order": item.order,
    #             "image_preview": item.sense.image_preview,
    #             "contents": get_user_lang_sense(item.sense.language_code, user_language_code, item.sense.contents or item.sense.original.contents, item.sense.id)
    #         } for item in items
    #     ]
        
    #     return Response(data)
    


    # def get_queryset(self):
    #     # Nếu muốn lọc theo người dùng hoặc trạng thái
    #     queryset = super().get_queryset()
    #     user = self.request.user if self.request.user.is_authenticated else None

    #     # Ví dụ: chỉ lấy các collection đang hoạt động
    #     queryset = queryset.filter(is_active=True)

    #     # Nếu muốn lọc theo người tạo (nếu có trường user), bạn có thể thêm:
    #     # if user:
    #     #     queryset = queryset.filter(user=user)

    #     return queryset

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

import re

def normalize(word: str) -> str:
    if not word:
        return ""
    
    # 1. Đưa về lowercase
    word = word.lower()
    
    # 2. Loại bỏ các ký tự đặc biệt
    # Giữ lại: chữ cái, số, khoảng trắng và các ký tự: ' - , .
    # Biểu thức chính quy: [^...] nghĩa là "không phải những ký tự này"
    # \w bao gồm chữ cái và số (hỗ trợ tốt cả tiếng Việt có dấu)
    word = re.sub(r"[^\w\s'\-\,\.]", "", word)
    
    # 3. Xoá khoảng trắng thừa bên trong và 2 bên
    # \s+ khớp với một hoặc nhiều khoảng trắng, tab, xuống dòng
    word = " ".join(word.split())
    
    return word
# Hàm này khác với hàm dịch tất cả các sense của AISENSE vì hàm này là chứa nhiều loại từ chứ không phải 1, do đó sẽ cần gửi từng sense + definition + pos của từ đó
def detect_missing(lang_code, user_lang_code, senses):
    word_ids =[]
    missings = []
    all_senses = senses
    for sense in senses:
        sense.contents, missing = get_user_lang_sense(sense.language_code, user_lang_code, sense.contents or sense.original.contents, sense.id)
        if missing:
            missings.append(missing)
            word_ids.append(str(sense.word_id))

    if missings:
        # Unique (word, user_language_code with 1 status PROCESSING allowed)
        try:
            r_queue = redis.Redis(host='redis', port=6379, db=0)

            # Đẩy vào queue "redis_word"
            r_queue.rpush("redis_trans", json.dumps({
                "word_id": word_ids,
                "trunk":1,
                "language_code": lang_code,
                "user_language_code": user_lang_code,
                "missing_translate": missings,
                'current_senses':all_senses
            }))
            # translate_instance = TranslateLog.objects.create(word=word_instance, language_code=user_language_code, status="PROCESSING")
            # background_task(render_translate(user, translate_instance, word, senses_instance, missing_contents , need_translation, language_code, user_language_code, socket_room))
        except:
            pass

        return missings

    return None