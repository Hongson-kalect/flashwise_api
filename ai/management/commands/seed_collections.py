"""
Tạo các bộ từ chính thức (hiện ở Khám phá, học được ngay) từ các nghĩa đã seed: mỗi chủ đề là một danh sách từ, lấy nghĩa chính thức đầu tiên
(không thô tục, có bản dịch tiếng Việt). Chạy lại an toàn: bộ chính thức trùng tên thì bỏ qua. Từ chưa có trong DB (vd từ nhiều nghĩa chưa seed) bị bỏ,
chủ đề còn dưới 8 từ thì không tạo.

  manage.py seed_collections
"""
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

THEMES = [
 ("Gia đình và con người","Người thân, bạn bè và các từ chỉ người trong đời sống hằng ngày.",
  "mother father brother sister uncle aunt son daughter wife husband baby child boy girl man woman family friend parents people kid neighbor teacher doctor king".split()),
 ("Nhà cửa và đồ vật","Các vật dụng, bộ phận trong nhà và đồ dùng quen thuộc.",
  "door window table chair house room kitchen bed floor wall key light clock car bag box book paper phone picture cup glass bottle knife plate roof garden".split()),
 ("Thời gian và tần suất","Ngày, giờ, mùa và các từ chỉ mức độ thường xuyên.",
  "time day night morning evening week month year hour minute today tomorrow yesterday past future now always never sometimes often early late soon again while".split()),
 ("Cảm xúc và tính từ thường gặp","Mô tả cảm xúc, trạng thái và tính chất của người và vật.",
  "happy sad angry mad glad afraid tired sick hungry sorry nice poor rich beautiful strong weak special serious perfect crazy lucky proud shy quiet calm busy".split()),
 ("Động từ cơ bản","Những động từ dùng nhiều nhất trong giao tiếp.",
  "go come take make give get see say think know want find tell ask work call try need feel leave keep let put show turn move live believe hold bring happen write sit stand lose".split()),
 ("Thiên nhiên và thế giới","Nước, đất, bầu trời, động vật và cảnh vật xung quanh.",
  "river water earth sun moon tree sea sky air fire rain wind snow mountain road bird dog cat horse fish flower stone light world island forest".split()),
 ("Số và số lượng","Chữ số, đơn vị và các từ chỉ số lượng.",
  "one two three four five six seven eight nine ten hundred thousand million half double number few many much some any every enough".split()),
]


class Command(BaseCommand):
    help = 'Tạo bộ từ chính thức theo chủ đề từ dữ liệu đã seed'

    def handle(self, *args, **options):
        G = lambda n: [m for m in apps.get_models() if m.__name__ == n][0]
        C, I, S, W = G('Collection'), G('CollectionItem'), G('AISense'), G('AIWord')
        admin=get_user_model().objects.filter(is_superuser=True).order_by('id').first()
        made=[]
        for name,desc,words in THEMES:
            if C.objects.filter(name=name,is_official=True,is_deleted=False).exists():
                print('đã có',name); continue
            picks=[]; seen=set(); missing=[]
            for w in words:
                if w in seen: continue
                seen.add(w)
                wd=W.objects.filter(value=w,language_code='en',status='COMPLETED').first()
                if not wd: missing.append(w); continue
                s=(S.objects.filter(word=wd,is_official=True).exclude(is_deleted=True).exclude(is_offensive=True).order_by('created_at','id').first())
                if s is None or not (s.contents or {}).get('definition',{}).get('vi'): missing.append(w+'*'); continue
                picks.append((w,s))
            print(name,len(picks),'từ; thiếu:',missing)
            if len(picks)<8: print('  bỏ qua (quá ít)'); continue
            with transaction.atomic():
                c=C.objects.create(name=name,description=desc,language_code='en',released=True,is_uploaded=True,is_official=True,is_active=True,
                                   version=1,item_count=len(picks),score=100,created_by=admin)
                I.objects.bulk_create([I(collection=c,original=s.original or s,sense=s,value=w,display_order=float(i),is_base=True,is_deleted=False,released=True,version=1,created_by=admin) for i,(w,s) in enumerate(picks,1)])
            made.append((name,len(picks),str(c.id)))
        print('TẠO',made)

