from django.db.models import Prefetch, Count
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from rest_framework.decorators import action
from core.models.Collection import Collection
from core.models.CollectionItem import CollectionItem
from core.models.UserCollection import UserCollection
from core.serializers.Collection import CollectionSerializer,CollectionDetailSerializer,CollectionListSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.sense_handle import get_user_lang_sense
from django.db import transaction
from django.contrib.auth.models import User

class CollectionViewSet(SoftDeleteViewSet):
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer
    # permission_classes = [IsAuthenticatedOrReadOnly]
    permission_classes = [AllowAny] # testing

    # override create request
    # hàm này create dựa vào các sense_id có sẵn
    def create(self, request, *args, **kwargs):
        # params: name, description, image, tags, language_code, senses
        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]

        data = request.data
        senses_data = data.pop('senses', []) # Giả sử nhận một list sense IDs {id, o_id}

        # Sử dụng atomic để đảm bảo tính toàn vẹn dữ liệu
        with transaction.atomic():
            # 1. Lưu Collection trước
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            collection = serializer.save()

            # 2. Chuẩn bị dữ liệu cho bảng phụ (CollectionItem)
            items_to_create = []
            for idx, sense_obj in enumerate(senses_data):
                items_to_create.append(
                    CollectionItem(
                        collection=collection,
                        sense_id=sense_obj.get('id'), # Truyền ID trực tiếp để tránh query thêm
                        original_id=sense_obj.get('o_id'), # Truyền ID trực tiếp để tránh query thêm
                        order=idx # Gán order theo thứ tự trong list gửi
                    )
                )

            # 3. Dùng bulk_create để "bắn" toàn bộ item vào DB trong 1 câu Query
            if items_to_create:
                CollectionItem.objects.bulk_create(items_to_create)

            UserCollection.objects.create(
                user=user,
                name=data["name"],
                description=data["description"],
                collection=collection,
                created_by=user,
            )

            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    #create collection by list of sense, regardless it exist or not
    @action(detail=False, methods=['post'], url_path='bulk-create')
    def create_bulk(self, request, *args, **kwargs):
        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]
        data = request.data
        senses_data = data.pop('senses', []) # Giả sử nhận một list sense IDs {id, o_id}
        
        # Khởi tạo 1 usercollection.
        # 1 lấy tất cả các sense hợp lệ đầu tiên của word với score cao nhất
        # Kiểm tra những từ không hợp lệ
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
            return CollectionDetailSerializer
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

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        user_language_code = request.query_params.get('user_lang', 'en')
        
        # Nếu muốn trả về format tùy chỉnh kèm danh sách senses
        data = serializer.data
        items = instance.collectionitem_set.all()
        
        # Format lại danh sách sense để client dễ dùng
        data['senses'] = [
            {
                "id": item.sense.id,
                "order": item.order,
                "image_preview": item.sense.image_preview,
                "contents": get_user_lang_sense(item.sense.language_code, user_language_code, item.sense.contents or item.sense.original.contents, item.sense.id)
            } for item in items
        ]
        
        return Response(data)
    


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
