# Flashwise API

Backend Django cho Flashwise: app học từ vựng bằng flashcard, cặp ngôn ngữ bất kỳ. Tra từ mới thì Gemini tạo nội dung, lưu Postgres để lần sau dùng lại cho mọi user. App (Expo, local-first SQLite) ở `D:\Code\flashwise`, xem CLAUDE.md bên đó.

## Stack
Django 4.2 + DRF + simplejwt, channels (websocket báo kết quả), Postgres 15 (cần extension pg_trgm), redis-stack (RedisJSON), google-genai. Chạy bằng `docker-compose.yml` (db, redis, web, worker, ai_worker), secret trong `.env` (mẫu: `.env.example`).

## Luồng chính
`GET /api/ai-words/get-word/?value&lang&user_lang` (`ai/views/AIWord.py`): Redis cache (đủ `languages` thì trả luôn) → Postgres (`ai/queries.py`, 2 query) → chưa có thì tạo `AIWord` PROCESSING và đẩy job vào list Redis `redis_word` (thiếu bản dịch thì `redis_trans`, khoá 120s/từ/ngôn ngữ để không đẩy trùng). Từ `REJECTED` trả 404, `FAILED`/PROCESSING quá `WORD_PROCESSING_STALE_SECONDS` được tạo lại trên cùng bản ghi. `utils/celery/worker.py` (asyncio, không phải Celery) đọc queue và chạy `ai_create_new_word_sema` (`utils/ai/word_render.py`).

### Tạo từ 2 bước AI
1. **Draft** (`GEMINI_API_MODEL`, model rẻ): stream từng nghĩa → socket `PARTIAL_SENSE` cho người tra đầu tiên (nháp KHÔNG vào Redis, app cũng không lưu SQLite bản stream này), song song dịch (`translate.py`) và sinh metadata (`metadata.py`) cho nghĩa đó. Draft không bao giờ vào DB.
2. **Refine** (`utils/ai/refine.py`, `GEMINI_REFINE_MODELS`, model mạnh hơn): gửi cả bản draft (chỉ ngôn ngữ gốc) để kiểm tra / chuẩn hoá pos, level, register, IPA, ví dụ, bỏ nghĩa trùng; chạy song song với việc dịch của bước 1. Phần bị sửa thì bỏ bản dịch cũ và dịch lại, đổi nghĩa / loại từ thì sinh lại metadata. Sau đó mới ghi DB (`quality_status=refined`, `ai_pass=2`, `ai_model`, `ai_review`). Refine lỗi / tắt (`AI_REFINE_ENABLED=False`) thì lưu draft (`quality_status=draft`).
- Đã chạy Gemini thật (bank, run, light, 猫): bước 2 chuẩn hoá đúng vai trò: bỏ câu ví dụ nhét trong `usage` (refine trả `usage: ""` thì xoá hẳn usage + bản dịch của nó), chuẩn hoá IPA (`l aɪ t` -> `/laɪt/`), sửa ví dụ không chứa từ đang tra; phần model giữ nguyên thì `ai_review.unchanged`. Từ vô nghĩa bị REJECTED ngay ở bước 1 (~1s, không gọi bước 2). Mỗi nghĩa được dịch sẵn sang `BASE_LANGUAGE` (10 ngôn ngữ) trong 1 lần gọi.
- Mốc thời gian điển hình (Gemini thật, từ ≤3 nghĩa): nghĩa đầu hiện sau ~1,5–2s, stream xong ~2s; mỗi nghĩa metadata ~1,1s + dịch 10 ngôn ngữ ~2–4s (song song); refine ~4–6s chạy cùng lúc nên là đường găng; dịch lại phần refine sửa thêm ~1,3–1,7s; tổng 6–10s. `ai_create_new_word_sema` log 1 dòng mốc (`first_sense, stream_end, background_done, refine_response, applied, saved`).
- Đo 6 từ ≤5 nghĩa (dog, cold, garden, milk, cheap, pencil): refine kéo dài thêm ~2,6s khi giữ nguyên, ~7–10s khi sửa nội dung / thêm nghĩa (phải dịch lại 10 ngôn ngữ + metadata); tổng 8–15s so với 5–7s nếu bỏ refine. Giá trị lớn nhất: sửa nghĩa sai (cheap: "Generous" -> stingy), bỏ nghĩa bịa (apple, window), bỏ ghi chú ngữ pháp sai. Prompt refine cấm diễn đạt lại nội dung đã đúng (mỗi thay đổi tốn một lần dịch lại) và buộc xoá nghĩa trùng sau khi sửa. Điểm yếu còn lại nằm ở `translations` (từ tương đương) do model rẻ sinh trong bước dịch, đôi khi sai hẳn (cold -> 'Không có', 'Trống') và refine không duyệt phần này.
- Chờ dịch / metadata của bước 1 có trần `AI_BACKGROUND_TIMEOUT` (90s): quá hạn thì huỷ phần treo, vẫn lưu nghĩa, bản dịch thiếu được get-word dịch bù. Cache Redis và socket là best-effort (`utils/utils/best_effort.py`): lỗi cache không được làm mất bản dịch / refine. Refine lỗi giữa chừng thì trả đúng bản draft nguyên vẹn (deepcopy), không nửa draft nửa refined. So sánh "có đổi không" của refine bỏ qua khác biệt Unicode / khoảng trắng (`_same`).
- Socket: `PARTIAL_SENSE` (gộp theo id, lần sau cho cùng id là bản chuẩn hoá), `PARTIAL_TRANSLATE`, `PARTIAL_METADATA`, `SENSE_REMOVED` (id nghĩa bị loại), `FULL_SENSE` (chốt, dữ liệu cuối), `REJECTED` / `ERROR` (kết thúc không có nghĩa). Cache Redis `status`: PROCESSING → CACHED / NOT_FOUND / ERROR; khi PROCESSING có `stage`: `draft` (bước 1 đang sinh + dịch) → `refine` (bước 2), `senses` rỗng cho tới khi có bản đã chuẩn hoá (`cache_word` ghi 1 lần lúc CACHED). Người tra giữa chừng thấy 202 + stage, nghĩa về qua socket (`FULL_SENSE` chốt). Bước 2 chỉ kiểm tra / sửa / xoá nghĩa của draft; khả năng bổ sung nghĩa còn thiếu (`action=add`) có sẵn nhưng mặc định TẮT (`AI_REFINE_ALLOW_ADD=False`) vì nghĩa thêm vào không có bước nào kiểm lại (đo thật: có khi thêm nghĩa hiếm hoặc gần trùng). Bật thì tối đa `AI_REFINE_MAX_ADD`=3 nghĩa mỗi từ, bỏ nếu thiếu pos / definition / example hoặc trùng nghĩa có sẵn; nghĩa mới được dịch + sinh metadata + gửi `PARTIAL_SENSE` với id mới, `ai_review.added`. Không có trần tổng số nghĩa (nhiều từ có nhiều nghĩa là thật).

## API cho app (ngoài get-word và collections)
- `POST /api/sync/push/` `{changes: {bảng: [dòng]}}` và `GET /api/sync/pull/?tables&limit&cursors={bảng: con trỏ}` (`tracking/sync.py`, `tracking/views/sync.py`). Bảng: user_sense_progress, review_log, day_learn_log, learn_session, user_note, user_settings, user_profile (chỉ push). Luật gộp: progress/note/setting bản `client_updated_at` mới hơn thắng (progress còn xét `last_reviewed_at`), review_log chỉ thêm, day_learn_log lấy max từng bộ đếm, session ghi đè. Push idempotent, dòng sai bị từ chối riêng; pull có tombstone `is_deleted`, con trỏ `thời điểm|id`. Push day_learn_log thì tính lại total_xp / streak của profile (`refresh_stats`, theo múi giờ user).
- `GET/PATCH /api/me/` (chỉ đổi thông tin cá nhân + ngôn ngữ, `clean_profile`), `GET /api/app-config/` (min_version, public).
- `POST/DELETE /api/collections/{id}/like/`, `POST /api/reports/`, `POST /api/feedback/` (throttle `feedback` 20/ngày).

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
- Bộ từ cá nhân dùng mô hình bản nháp / phát hành trên `CollectionItem`: dòng đã phát hành (`released`, `version`) không bị sửa, mọi thay đổi là dòng nháp của user (sửa nghĩa = dòng trỏ nghĩa mới, xoá = dòng `is_deleted` "bia mộ"), cùng `original_id` thì nháp thắng bản phát hành (`order_by released` tăng dần trước `distinct`). `upload` phát hành nháp (chủ bộ: tăng version, công khai bộ, đóng băng nghĩa; người khác: tách thành bộ remix). Ràng buộc unique chỉ áp cho dòng đã phát hành (`collection, sense, version`). `upload` hiện công khai ngay, chưa có bước duyệt.
- Code Celery cũ (`utils/celery/word.py`, `utils/utils/ai.py`, `ai_create_new_word` trong word_render.py, service `worker` trong compose) không còn được get-word dùng và đã hỏng (sai chữ ký `render_word_prompt`), có thể xoá.
- Mỗi nghĩa vẫn gọi riêng 1 lần dịch + 1 lần metadata; có thể gom theo từ khi không cần hiện từng nghĩa ngay.
- Chưa có thuật toán FSRS phía server (app tự tính khi chấm thẻ, server chỉ lưu). Chưa sync `user_collection` (tiến độ bộ từ) và `collection_items` của bộ cá nhân.
