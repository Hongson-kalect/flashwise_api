# Flashwise API

Backend Django cho Flashwise: app học từ vựng bằng flashcard, cặp ngôn ngữ bất kỳ. Tra từ mới thì Gemini tạo nội dung, lưu Postgres để lần sau dùng lại cho mọi user. App (Expo, local-first SQLite) ở `D:\Code\flashwise`, xem CLAUDE.md bên đó.

## Stack
Django 4.2 + DRF + simplejwt, channels (websocket báo kết quả), Postgres 15 (cần extension pg_trgm), redis-stack (RedisJSON), google-genai. Chạy bằng `docker-compose.yml` (db, redis, web, worker, ai_worker), secret trong `.env` (mẫu: `.env.example`).

## Luồng chính
`GET /api/ai-words/get-word/?value&lang&user_lang` (`ai/views/AIWord.py`): Redis cache → Postgres → chưa có thì tạo `AIWord` PROCESSING và đẩy job vào list Redis `redis_word` (thiếu bản dịch thì `redis_trans`). `utils/celery/worker.py` (asyncio, không phải Celery) đọc queue, gọi Gemini (`utils/ai/word_render.py`, `translate.py`, `prompt.py`, `schema.py`), ghi DB theo mẻ qua `db_buffer:*`.

## App Django
- `ai`: AIWord, AISense (nội dung đa ngôn ngữ trong JSON `contents`, versioning original/previous, `quality_status`/`ai_pass` cho pipeline sinh 2 lần), AISenseMetadata, TranslateLog
- `core`: Collection, CollectionItem (`display_order`), UserCollection, Language, Quiz, ImageLibrary
- `tracking`: UserSenseProgress (FSRS), ReviewLog (mỗi lần chấm thẻ), DayLearnLog
- `interact`: LearnSession, UserNote, UserInteraction, Report, Feedback, UpdateLog
- `user`: UserProfile (tier guest/normal_user/VIP), Device, UserSession (jti refresh token), `views/auth.py` (device-login, refresh-token xoay vòng, logout)
- `cache`, `asset` (Theme, Image), `utils` (Ruby kanji, Tag, permissions, soft delete viewset)

## Quy ước
- Comment tiếng Việt. PK UUIDv7. Model kế thừa `config.models.BaseModel` (soft delete, created_by...).
- Tên trường bảng nào sync với app phải giống cột SQLite trong `flashwise/database/schema/*.ts`.
- Quyền: `utils/utils/permissions.py` (IsAdminOrReadOnly, IsOwnerOrAdmin, WordGenerateThrottle, get_request_user). Không gán cứng user.

## Lệnh, kiểm tra, commit
Dùng skill `backend-verify` (check, makemigrations, test trong Docker, commit giữ đúng CRLF/LF).

## Còn dở
- Chưa có sync API cho progress / review log / note (model + serializer đã có).
- Chưa có pipeline sinh 2 lần (mới có trường), chưa có thuật toán FSRS.
- `AIWord.get_word` còn đoạn code chết sau các `return`.
