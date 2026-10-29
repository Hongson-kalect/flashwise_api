# Flashwise API

Backend Django cho Flashwise: app học từ vựng bằng flashcard, cặp ngôn ngữ bất kỳ. Tra từ mới thì Gemini tạo nội dung, lưu Postgres để lần sau dùng lại cho mọi user. App (Expo, local-first SQLite) ở `D:\Code\flashwise`, xem CLAUDE.md bên đó.

## Stack
Django 4.2 + DRF + simplejwt, channels (websocket báo kết quả), Postgres 15 (cần extension pg_trgm), redis-stack (RedisJSON), google-genai. Chạy bằng `docker-compose.yml` (db, redis, web, worker, ai_worker), secret trong `.env` (mẫu: `.env.example`).

## Luồng chính
`GET /api/ai-words/get-word/?value&lang&user_lang` (`ai/views/AIWord.py`): Redis cache (đủ `languages` thì trả luôn) → Postgres (`ai/queries.py`, 2 query) → chưa có thì tạo `AIWord` PROCESSING và đẩy job vào list Redis `redis_word` (thiếu bản dịch thì `redis_trans`, khoá 120s/từ/ngôn ngữ để không đẩy trùng). Từ `REJECTED` trả 404, `FAILED`/PROCESSING quá `WORD_PROCESSING_STALE_SECONDS` được tạo lại trên cùng bản ghi. `utils/celery/worker.py` (asyncio, không phải Celery) đọc queue và chạy `ai_create_new_word_sema` (`utils/ai/word_render.py`).

### Tạo từ 2 bước AI
1. **Draft** (`GEMINI_API_MODEL`, model rẻ): stream từng nghĩa → socket `PARTIAL_SENSE` + cache ngay cho người tra đầu tiên, song song dịch (`translate.py`) và sinh metadata (`metadata.py`) cho nghĩa đó. Draft không bao giờ vào DB.
2. **Refine** (`utils/ai/refine.py`, `GEMINI_REFINE_MODELS`, model mạnh hơn): gửi cả bản draft (chỉ ngôn ngữ gốc) để kiểm tra / chuẩn hoá pos, level, register, IPA, ví dụ, bỏ nghĩa trùng; chạy song song với việc dịch của bước 1. Phần bị sửa thì bỏ bản dịch cũ và dịch lại, đổi nghĩa / loại từ thì sinh lại metadata. Sau đó mới ghi DB (`quality_status=refined`, `ai_pass=2`, `ai_model`, `ai_review`). Refine lỗi / tắt (`AI_REFINE_ENABLED=False`) thì lưu draft (`quality_status=draft`).
- Socket: `PARTIAL_SENSE` (gộp theo id, lần sau cho cùng id là bản chuẩn hoá), `PARTIAL_TRANSLATE`, `PARTIAL_METADATA`, `SENSE_REMOVED` (id nghĩa bị loại), `FULL_SENSE` (chốt, dữ liệu cuối), `REJECTED` / `ERROR` (kết thúc không có nghĩa). Cache Redis `status`: PROCESSING → CACHED / NOT_FOUND / ERROR.

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
- Tiếng Nhật dùng schema phẳng như Latin + `roman` (AI) + `ruby` (fugashi, dạng `[漢字|かな]`); refine đổi văn bản thì `roman` của đoạn đó bị bỏ. zh/ko/... dùng schema phẳng thuần (chưa có reading / pinyin). `nonlatin_schema` cũ không còn dùng.
- `user-collections`: `update-sense` và `delete-sense` chưa chạy được. Cơ chế bản nháp / đã phát hành tạo thêm dòng CollectionItem cho cùng một nghĩa nhưng `unique_together (collection, sense)` cấm; `update-sense` còn gán dict vào FK metadata và `list.append(...)` thành None. Cần quyết định thiết kế (nới ràng buộc hay đổi cách lưu phiên bản) rồi mới sửa; app đang local-first nên chưa gọi.
- Code Celery cũ (`utils/celery/word.py`, `utils/utils/ai.py`, `ai_create_new_word` trong word_render.py, service `worker` trong compose) không còn được get-word dùng và đã hỏng (sai chữ ký `render_word_prompt`), có thể xoá.
- Mỗi nghĩa vẫn gọi riêng 1 lần dịch + 1 lần metadata; có thể gom theo từ khi không cần hiện từng nghĩa ngay.
- Chưa có sync API cho progress / review log / note (model + serializer đã có), chưa có thuật toán FSRS.
