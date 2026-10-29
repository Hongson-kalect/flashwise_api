---
name: backend-verify
description: Kiểm tra và commit thay đổi backend flashwise_api - Django check, makemigrations, migrate + test trên Postgres trong Docker, commit giữ đúng CRLF/LF. Dùng sau khi sửa model, serializer, view.
---

# Kiểm tra và commit backend

## 1. Kiểm tra nhanh trên máy (không cần Docker)
Venv Python 3.10 ở `C:/Users/mouse/.fwvenv` (đường dẫn phải ngắn, Windows lỗi path dài). Chưa có thì: `py -3.10 -m venv C:/Users/mouse/.fwvenv && C:/Users/mouse/.fwvenv/Scripts/python -m pip install -r requirements.txt`.

```bash
export GEMINI_API_KEY=dummy DJANGO_DEBUG=True   # utils/utils/ai.py tạo genai client lúc import
P=C:/Users/mouse/.fwvenv/Scripts/python
$P manage.py check
$P manage.py makemigrations --check --dry-run    # phải ra "No changes detected" (cảnh báo không kết nối db là bình thường)
```

`makemigrations` hỏi đổi tên trường bằng input() nên chạy không tương tác sẽ lỗi EOF. Trả lời có kiểm soát:
```python
from django.db.migrations import questioner as q
q.InteractiveMigrationQuestioner.ask_rename = lambda self, m, o, n, f: (o, n) in {("ten_cu", "ten_moi")}
q.InteractiveMigrationQuestioner.ask_rename_model = lambda self, o, n: False
# thêm FK bắt buộc vào bảng có dữ liệu: trả models.NOT_PROVIDED rồi tự thêm RunSQL xoá / điền dữ liệu vào migration
from django.core.management import call_command; call_command("makemigrations")
```
(chạy sau `os.environ.setdefault('DJANGO_SETTINGS_MODULE','flashcardApi.settings'); django.setup()`)

## 2. Migrate + test trên Postgres thật
Cần Docker Desktop đang chạy và file `.env`. Dùng project riêng `fwtest`; Git Bash cần `MSYS_NO_PATHCONV=1` cho `exec`.
```bash
docker compose -p fwtest up -d --build db redis web
MSYS_NO_PATHCONV=1 docker compose -p fwtest exec -T web python manage.py migrate
MSYS_NO_PATHCONV=1 docker compose -p fwtest exec -T -e DJANGO_DEBUG=True web python manage.py test user.tests   # nhãn module, không dùng "user"
docker compose -p fwtest stop
```
Muốn DB test sạch: dừng db, xoá `postgres_data/` (chỉ khi chắc đó là DB test), bật lại.
Docker báo lỗi `dockerInference` là lỗi Model Runner, không chặn engine; đã tắt `EnableInference` trong `%APPDATA%\Docker\settings-store.json`.

## 3. Commit (repo trộn file CRLF và LF)
Sửa bằng sed / python dễ đổi CRLF thành LF làm diff cả file. Trước khi commit: đưa mỗi file sửa về kiểu xuống dòng như ở HEAD, rồi stage không cho git đổi.
```bash
git diff --name-only --diff-filter=M HEAD | $P -c "
import sys,subprocess
for f in sys.stdin.read().split():
    old=subprocess.run(['git','show','HEAD:'+f],capture_output=True).stdout
    b=open(f,'rb').read().replace(b'\r\n',b'\n')
    if b'\r\n' in old: b=b.replace(b'\n',b'\r\n')
    open(f,'wb').write(b)"
git -c core.autocrlf=false add -A
git diff --cached --stat   # số dòng phải khớp phần thực sự sửa
git -c core.autocrlf=false commit
```
Không commit `.env`. Commit trên nhánh riêng, không push khi chưa được hỏi.
