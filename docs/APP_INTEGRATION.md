# Flashwise backend: hướng dẫn kết nối cho app

Tóm tắt những gì backend (`D:\Code\flashwise_api`, nhánh `refactor/models-review`, cập nhật 2026-11-02) đã có, để dựng
front-end (`D:\Code\flashwise`, Expo) kết nối và test. Chi tiết cài đặt / thuật toán nằm ở `CLAUDE.md` của backend;
file này viết theo góc nhìn app: gọi gì, gửi gì, nhận gì, lỗi gì.

Quy ước chung:
- Mọi API dưới `http://<host>:8000/api/`. Body và kết quả là JSON (trừ upload file: multipart).
- Header đăng nhập: `Authorization: Bearer <accessToken>`.
- Lỗi có mã máy đọc được ở `errorCode` (vd `{"detail": "...", "errorCode": "user_banned"}`); app nên rẽ nhánh theo `errorCode`, không theo `detail`.
- Id là UUIDv7 dạng chuỗi. Thời gian là ISO 8601 (UTC).
- Danh sách có phân trang (`collections/`, `user-collections/`): `?page=2`, trả `{count, next, previous, results}`, 50 dòng / trang.
- Tên cột các bảng trả về cho SQLite (bundle, sync, learn) trùng tên cột trong `flashwise/database/schema/*.ts`.

---

## 1. Chạy backend để test

```bash
cd D:\Code\flashwise_api
copy .env.example .env        # rồi điền theo hướng dẫn trong file (tối thiểu DJANGO_SECRET_KEY, POSTGRES_PASSWORD; tạo từ mới cần ANTHROPIC_API_KEY + GEMINI_API_KEY)
docker compose up -d db redis web ai_worker
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser        # tài khoản admin (Django admin: http://localhost:8000/admin/)
docker compose exec web python manage.py ai_words_snapshot import   # nạp sẵn 19 từ / 51 nghĩa mẫu (fixtures/ai_words.json), khỏi tốn AI
```
- Không chạy service `worker` (Celery cũ, đã hỏng) và `pgadmin` (chỉ để xem DB).
- **Điện thoại thật**: đặt `DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,<IP LAN>` và `MEDIA_PUBLIC_URL=http://<IP LAN>:8000/upload/` trong `.env`, app gọi `http://<IP LAN>:8000/api/`. Android emulator: `10.0.2.2`.
- **Token test nhanh** (chỉ khi `DJANGO_DEBUG=True`): `GET /api/auth/dev-token/?username=<tên>` (bỏ trống = superuser đầu tiên) trả `{accessToken, refreshToken}`.
- Log: `docker compose logs -f web ai_worker`. Mail đặt lại mật khẩu (chưa có SMTP) in ở log `web`.
- Seed nhiều từ: `docker compose exec web python manage.py seed_words <file> --lang en --base-languages en,vi --rate 60` (file mỗi dòng 1 từ).
- Test backend: skill `backend-verify` (Docker project `fwtest`).

Key ngoài chưa có (Google, Apple, SMTP, R2, Pixabay) thì tính năng tương ứng trả 503 hoặc tắt, phần còn lại chạy bình thường.

---

## 2. Khởi động app

1. `GET /api/app-config/` (không cần token) → `{min_version, latest_version, media_base_url}`.
   - Số phiên bản app < `min_version`: bắt cập nhật.
   - `media_base_url`: ghép với mọi khoá file (ảnh, audio) server trả về. Giá trị đã bắt đầu bằng `http(s)://` thì dùng nguyên.
     Vd `image_preview = "images/v1/pixabay_1.webp"` → `media_base_url + image_preview`.
2. Chưa có token: `POST /api/auth/device-login/` (mục 3). Có token: dùng tiếp, 401 thì refresh.
3. `GET /api/me/` lấy hồ sơ; `GET /api/sync/pull/` kéo dữ liệu học về SQLite (mục 6).

---

## 3. Đăng nhập, tài khoản

Mọi API đăng nhập trả cùng dạng:
```json
{"accessToken": "...", "refreshToken": "...", "user_info": {hồ sơ, xem GET me}, "is_new": false, "linked": false, "switched_from_guest": false}
```
(`device-login` chỉ có 3 trường đầu.) Body các API đăng nhập luôn kèm thông tin thiết bị:
`device_id` (bắt buộc: UUID app tự sinh 1 lần, giữ trong SecureStore), `os`, `app_version`, `device_name`, `push_token`.

| API | Body | Ghi chú |
|---|---|---|
| `POST auth/device-login/` | thiết bị | Máy mới: tạo tài khoản khách (`tier: guest`). Máy đã gắn tài khoản thật: 401 `login_required` (phải đăng nhập lại bằng cách thật). Tài khoản bị khoá hẳn: 403 `account_disabled` |
| `POST refresh-token` (cả `refresh-token/`) | `{refreshToken}` | Trả cặp token mới; token cũ hết dùng được. 401 `token_invalid` / `token_revoked` → đăng nhập lại. Dùng lại refresh token cũ = nghi lộ, đóng mọi phiên của máy đó |
| `POST auth/logout/` | `{refreshToken?}` | Có token: đóng phiên đó; không: đóng mọi phiên. Xoá push_token của máy |
| `POST auth/social-login/` | thiết bị + `provider: google\|apple`, `id_token`, `full_name?`, `authorization_code?` (Apple) | App đăng nhập bằng SDK (expo-auth-session / expo-apple-authentication) rồi gửi id token. Chưa cấu hình key: 503 `provider_not_configured`. Token sai / hết hạn: 401 `token_invalid` |
| `POST auth/email/register/` | thiết bị + `email`, `password`, `full_name?` | 409 `email_in_use` / `email_already_set`, 400 `email_invalid` / `password_invalid` (kèm `errors[]`) |
| `POST auth/email/login/` | thiết bị + `email`, `password` | Sai: 401 `invalid_credentials` |
| `POST auth/password/forgot/` | `{email}` | Gửi mã 6 số (15 phút). Luôn 200 dù email không tồn tại. Chưa có mail: 503 `email_not_configured` |
| `POST auth/password/reset/` | `{email, code, password}` | Sai mã: 400 `code_invalid` (sai 5 lần thì mã huỷ). Đúng: đóng mọi phiên, đăng nhập lại |
| `POST auth/password/change/` | `{old_password, password}` | Tài khoản chưa có email: 400 `email_required` |
| `GET auth/providers/` | | `{providers: [{provider, email, linked_at}], configured: {google, apple, email}}`: dùng `configured` để ẩn nút đăng nhập chưa có key |
| `DELETE auth/providers/{provider}/` | | Gỡ 1 cách đăng nhập; còn 1 cách thì 400 `last_login_method` |

**Nâng cấp tài khoản khách**: gọi `social-login` / `email/register` / `email/login` KÈM token khách.
- Danh tính chưa ai dùng → gắn vào chính tài khoản khách (`linked: true`, tier lên `normal_user`), dữ liệu học giữ nguyên.
- Danh tính đã có chủ → chuyển sang tài khoản đó (`switched_from_guest: true`). Dữ liệu khách vẫn ở SQLite: app đẩy lại qua `sync/push` (luật gộp xử lý trùng).
- Email Google trùng email đã đăng ký mật khẩu (chưa đăng nhập): 409 `email_in_use` → bảo user đăng nhập email rồi liên kết Google.

**Hồ sơ, thiết bị, dữ liệu cá nhân**
- `GET /api/me/` → hồ sơ: `user, full_name, avatar_url, age, gender, dob, country_code, native_language, learning_languages, fav_time, level, total_xp, current_streak, max_streak, tier, tier_expired_at, time_zone, zone_num, is_active, ...`
- `PATCH /api/me/`: chỉ đổi thông tin cá nhân + ngôn ngữ (XP, streak, tier do server giữ). Sai: 400 kèm `errors`.
- `DELETE /api/me/` body `{"confirm": true}` (thiếu: 400 `confirm_required`): xoá ngay, không khôi phục. Trả số liệu đã xoá. Bộ đã phát hành vẫn còn (bỏ tên tác giả).
- `GET /api/me/export/`: file JSON toàn bộ dữ liệu (tối đa 5 lần / ngày).
- `POST /api/me/device/` `{device_id, push_token, os?, app_version?, device_name?}`: cập nhật push token (`""` = thôi nhận). Máy chưa đăng nhập: 404 `device_not_found`.

**Bị ban** (`user_info.is_active = false`): vẫn học, sync, tra từ, gửi góp ý; các việc công khai (phát hành bộ, like, report, upload file) trả 403 `{errorCode: "user_banned", ban_reason, ban_expires_at}`.

---

## 4. Tra từ (AI tạo từ mới) + websocket

`GET /api/ai-words/get-word/?value=<từ>&lang=<ngôn ngữ của từ>&user_lang=<ngôn ngữ người học>` (không cần token; có token thì ghép thêm nghĩa riêng của user).

Kết quả `{detail, status, data}`:
| HTTP | Ý nghĩa | `data` |
|---|---|---|
| 200 | Có đủ | từ + `senses` (object `{sense_id: nghĩa}`), nội dung chỉ ngôn ngữ gốc + `user_lang` |
| 200, `detail: "FORM_OF"` | Chuỗi là dạng biến cách (`killed`, `ran`) của từ gốc CHƯA có trong DB (chỉ en / ru / uk; không nhận nhãn so sánh / so sánh nhất vì nhận nhầm kiểu `number` → `numb`) | `{redirect: từ gốc, form_of: {form, lemma, pos, tag, own_entry: false, candidates}}`, không có `senses`. App tra tiếp `redirect` (get-word của từ gốc tạo online như từ mới, đăng ký phòng socket của TỪ GỐC) và giữ `form_of` để hiện "là dạng … của …" + ví dụ riêng của dạng đã tra (`ai-senses/form-example`). Server không tạo mục riêng cho dạng chia. Từ gốc đã có thì get-word trả luôn từ gốc kèm `data.form_of` (không qua redirect) |
| 206 | Có từ nhưng thiếu bản dịch sang `user_lang` | như 200; phần dịch thiếu về qua socket `PARTIAL_TRANSLATE` / lần tra sau |
| 201 | Từ mới: vừa bắt đầu tạo bằng AI | `{id, value, language_code, senses}` → mở socket chờ |
| 202 | Đang tạo (người khác khởi tạo) | trạng thái cache, có `stage`: `draft` / `refine` → mở socket chờ |
| 404 | Từ không hợp lệ / bị từ chối / bị ban | `{value, language_code}` |
| 429 | Tra từ mới quá nhiều (`word_generate_throttled`, 60 / giờ) | |
| 400 | Chuỗi không phải mục từ (`errorCode: not_a_dictionary_entry`, `reason`: `sentence` / `too_long`): chỉ tra từ đơn, cụm từ, thành ngữ, tục ngữ; câu bất kỳ bị từ chối ngay, không tốn lượt AI (từ đã có trong DB vẫn trả bình thường). AI cũng từ chối câu ở bước tạo từ (về app như 404) | `{detail, errorCode, reason}` |
| 500 | Lần tạo vừa lỗi | `{retry_after}` giây sau thử lại |
| 503 | Hàng đợi / DB lỗi | |

**Websocket** `ws://<host>:8000/ws/word`:
- Gửi `{"action": "subscribe", "word_room": "<room>"}`; huỷ: `"action": "unsubscribe"`.
- `room = md5(chuẩn hoá(value)) + "_" + lang`; chuẩn hoá = trim, gộp khoảng trắng, chữ thường (giống `normalizeWord` của app).
- Tin nhắn nhận: `{"type": ..., "payload": ..., "word_room": ...}`, có `unsubscribe: true` ở tin cuối.

| type | payload | App làm gì |
|---|---|---|
| `WORD` | `{id, value, language_code}` | Từ hợp lệ, hiện khung từ |
| `PARTIAL_SENSE` | `{sense_id: nghĩa}` | Gộp theo id (lần sau cùng id = bản đã chuẩn hoá). Bản nháp, KHÔNG lưu SQLite |
| `PARTIAL_TRANSLATE`, `PARTIAL_METADATA`, `PARTIAL_IMAGE` | phần bổ sung theo nghĩa | Gộp vào nghĩa đang hiện |
| `SENSE_REMOVED` | `[sense_id]` | Gỡ các nghĩa đó |
| `FULL_SENSE` | từ đầy đủ (như get-word 200) | Bản cuối: thay toàn bộ, lưu được |
| `REJECTED` / `ERROR` | thông báo | Kết thúc, không có nghĩa |

Từ khác:
- `GET /api/ai-words/search/?q&lang&limit` (≤50): gợi ý khi gõ, chỉ từ đã có. Trả `[{id, value, language_code}]`.
- Nghĩa chính và thứ tự nghĩa: `POST/DELETE /api/ai-senses/{id}/main/` → `{main, sense_id, word_id, main_count}` đặt / bỏ nghĩa này làm nghĩa chính của từ cho mình (mỗi từ 1 nghĩa chính, đặt nghĩa khác thì phiếu cũ thu hồi). Tài khoản thật còn cộng vào điểm chung của nghĩa chính thức (`main_count`, `score`), khách chỉ có tác dụng riêng. `get-word` trả `senses` đã xếp sẵn: nghĩa chính của mình, rồi nghĩa mình tự sửa / thêm, rồi theo loại từ (điểm cao nhất của loại đó) và `score` (mỗi nghĩa có `score` và `recommended` = admin đề xuất, app hiện huy hiệu; riêng nghĩa chính của người đang đăng nhập có thêm `is_main: true`, app hiện sao vàng; app giữ nguyên thứ tự server trả, không tự sắp lại, và sau khi đặt nghĩa chính nên tải lại từ rồi giữ người dùng ở đúng nghĩa đang xem). `score` tổng hợp: admin đề xuất (rất mạnh), số người đặt chính, số người đã học; lượt xem / like không tính (`ai/scores.py`). Admin: `POST/DELETE /api/ai-senses/{id}/recommend/` đề xuất / gỡ đề xuất nghĩa chính thức. Đổi công thức: `manage.py refresh_sense_scores`.
- `GET /api/ai-words/senses/?lang&words=a,b,c&user_lang` (cần token, tối đa 20 từ, chỉ đọc, không tạo từ, không tính hạn mức): các nghĩa chính thức của từ để người dùng chọn khi thêm từ vào bộ (chế độ chuyên sâu). Trả `{results: {từ: {status, senses?}}}`, `status` như `ai-words/check`; từ `exists` có `senses: [{id, pos, level, like_count, definition, translations, example, image_preview, best}]` theo độ tốt giảm dần (`score`, rồi `like_count`, rồi nghĩa AI trả ra trước), `best` = nghĩa chế độ nhanh sẽ chọn. Chế độ nhanh: `collections/bulk-create` và `add-words` tự chọn đúng nghĩa `best` của mỗi từ; chế độ chuyên sâu: gọi endpoint này rồi `add-senses {sense_ids}` (một bộ được chứa nhiều nghĩa của cùng một từ). `score` xem mục nghĩa chính ở trên (chưa có tín hiệu nào thì bằng nhau, giữ thứ tự AI trả ra).
- `POST /api/ai-words/bulk-create/` `{lang, user_lang, words}` (cần token): tạo nhiều từ cùng lúc (từ mới được AI tạo dần). Câu / chuỗi quá dài vào `invalid` ngay; từ mới tính vào hạn mức `bulk_words` theo ngày (429 `bulk_quota_exceeded`, không tạo từ nào). Trả `{results: {từ chuẩn hoá: {status, reason?}}}`, `status`: `exists` | `processing` | `rejected` | `invalid` (`reason`: `sentence` / `too_long` / `not_a_word`) | `unknown`. Bộ từ (`collections/bulk-create`, `user-collections/{id}/add-words`) gọi lại cùng các hàm trong `ai/bulk.py`.
- `POST /api/ai-words/check/` `{lang, words (≤ 20)}` (cần token): hỏi tiến độ các từ, chỉ đọc (không tạo, không tính hạn mức), kết quả như bulk-create. App hỏi lại mỗi vài giây cho tới khi hết `processing`.
- Nghĩa (object trong `senses`) có: `id, word_id, original_id, pos, level, register, ipas, audio, image_preview, contents, delta, is_official, like_count, global_difficulty, learner_count, ...`.
  `contents`: `definition`, `usage` (`{lang: {value, ruby?, roman?}}`), `examples` (`[{lang: {value}}]`), `translations` (`{lang: [từ]}`), nội dung nâng cao (mục 5).
  Phát âm: `sense.ipas` rỗng thì `word.ipas`; âm thanh `sense.audio` > `word.audio` > giọng máy (TTS).
  **Phiên âm Latin (`roman`)** cho tiếng Nhật / Trung / Hàn do server sinh bằng thuật toán (`utils/ai/romanize.py`, AI không viết): ja = Hepburn (ō ū), zh = pinyin có dấu thanh, ko = Revised Romanization có biến âm. `roman` nằm ở `definition` / `usage` / từng `examples[i]` của ngôn ngữ của từ (`contents.definition.zh.roman`), và `ipas` của các ngôn ngữ này là 1 mục `{label: "READING", value, roman}` (ja: value = hiragana; zh: value = roman = pinyin; ko: value = Hangul). Ngôn ngữ khác `ipas` vẫn là IPA, không có `roman`. User sửa chữ thì server sinh lại `roman` (và `ruby` của tiếng Nhật), client không cần gửi.

---

### Tham số FSRS riêng

`GET /api/learn/fsrs/` → `{parameters: [21 số], meta}` (mặc định FSRS-6 nếu user chưa có tham số riêng). `POST /api/learn/fsrs/` tối ưu ngay từ `review_log` đã đồng bộ (tối đa 6 lần / giờ, 429 `fsrs_fit_throttled`) → `{status: not_enough | up_to_date | kept | updated, n_items, loss_before?, loss_after?, parameters, meta}`. Tham số riêng nằm ở `user_settings.fsrs_params` (21 số) và về app qua `sync/pull`; server dùng chúng khi tính lịch (`tracking/session.py`) và xếp thẻ ôn. Chạy hàng loạt: `manage.py fit_fsrs_params [--user ID] [--force]` (cron mỗi ngày).
"Tôi biết từ này" (`known: true`) vẫn lên cấp 5 / Easy nhưng hẹn ôn kiểm tra sau 1 ngày; ôn vuốt (`mode=quick`) không lấy thẻ cấp 5 mới chấm 1 lần.

## 5. Nghĩa riêng của user

| API | Ghi chú |
|---|---|
| `POST ai-senses/create-sense/` | **Chỉ admin** (người dùng thường 403): người dùng đề xuất nghĩa mới qua `ai-senses/propose/`, không thêm tự do. `{word_id, user_lang, contents, metadata: {}, audio?, image?}`; `contents.definition[ngôn ngữ của từ]` bắt buộc. 201, hoặc 206 khi đang dịch phần còn lại |
| `PUT ai-senses/update-sense/` | `{sense_id, user_lang, delta, metadata?, image?, audio?}`. Nghĩa chính thức / của người khác → tạo bản riêng mới (trả nghĩa mới, `original_id` = nghĩa gốc); nghĩa riêng của mình → sửa tại chỗ |
| `DELETE ai-senses/{id}/` | Nghĩa riêng chưa phát hành (đã nằm trong bộ phát hành: 409) |
| `POST/DELETE ai-senses/{id}/hide/` | Ẩn / hiện 1 nghĩa với riêng mình |
| `GET ai-senses/mine/?word_id` | Nghĩa riêng + id đang ẩn (`hidden`) + đang thích (`liked`) |
| `POST/DELETE ai-senses/{id}/like/` | `{liked, sense_id, like_count}` |
| `GET ai-senses/top/?lang&by=forgot\|forget_rate\|learned\|liked&limit&user_lang` | Nghĩa nổi bật toàn hệ thống (cache 10 phút) |
| `POST ai-senses/propose/` | Đề xuất nghĩa mới cho 1 từ, chỉ cần gõ định nghĩa. Body `{word_id, user_lang, definition (8-500 ký tự), pos?}`; chạy đồng bộ ~3-6s (đặt timeout request >= 30s). Cổng AI: Claude chấm + viết nội dung / bản dịch / metadata, Gemini kiểm chéo song song; Claude xong mà Gemini chưa về thì không chờ. Trả `{result}`: `official` (201, nghĩa chính thức ngay, có `sense`), `pending` (201, chờ admin duyệt, chỉ tác giả thấy, kèm `reasons`: `no_second_opinion`, `low_confidence`, `claude_uncertain`, `gemini_*`, `translation_problem`), `duplicate` (200, kèm nghĩa đã có trong `sense`), `rejected` (200, `reason` ngắn bằng tiếng Anh, `abuse` true nếu chèn lệnh / spam, bị trừ uy tín). Lỗi: 400 (tham số; tiếng Nhật / Trung / Hàn: `roman` (và furigana `ruby` của tiếng Nhật) do server sinh), 403 `account_required` (tài khoản khách; đổi bằng `SENSE_PROPOSE_ALLOW_GUEST`) / `user_banned`, 404 từ chưa có, 429 (10 lần / giờ), 503 `ai_unavailable`. Sau `official` / `pending` app làm mới get-word (server đã xoá cache) |
| `GET ai-senses/proposals/?status=pending\|approved&limit` (admin) | Hàng chờ duyệt nghĩa người dùng đề xuất, kèm phán quyết từng lớp AI. Duyệt: `POST ai-senses/{id}/promote/`; từ chối: `DELETE ai-senses/{id}/` |
| `GET ai-senses/community/?word_id&user_lang&ordering=popular\|newest&limit` | Gợi ý nghĩa của cộng đồng cho 1 từ (không cần token). Chỉ nghĩa tuỳ chỉnh người khác đã phát hành (đóng băng, nằm trong bộ đã phát hành), bỏ nghĩa xấu / bị flag; có token thì bỏ nghĩa của mình và nghĩa đã ẩn, báo `liked`. `popular` xếp theo `rank` = 3·ln(1+like) + 2·ln(1+số bản tách từ nghĩa này) + uy tín tác giả/10 (kẹp -5..10) + 3 nếu verified. Mỗi dòng: `id, pos, level, ipas, image_preview, contents` (đã gộp delta lên nghĩa gốc, chỉ ngôn ngữ của từ + `user_lang`), `metadata{advanced,tags}`, `like_count, adopted_count, quality_status, liked, author{id,name,reputation}, rank`. Chưa ai phát hành thì `senses` rỗng. App chọn 1 nghĩa thì chỉ điền các ô đang trống (không ghi đè) |

Nội dung nâng cao trong `contents` (user / admin gõ, AI không sinh): `pro_tip`, `cultural_note`, `etymology`, `grammar_note`,
`explanation` dạng `{lang: {value}}`; `synonyms`, `antonyms`, `related` là danh sách từ (≤20). Thiếu thì app dùng kiểu thẻ cơ bản.
`audio` / `image`: khoá file từ `POST media/` (mục 9) của chính mình (`image` nhận thêm URL http(s)); `""` = bỏ, không gửi = giữ.

---

## 6. Đồng bộ dữ liệu học (local-first)

`POST /api/sync/push/` body `{"changes": {bảng: [dòng]}}`, tối đa 500 dòng / bảng / lần, gửi lại an toàn (idempotent).
Bảng: `user_sense_progress`, `review_log`, `day_learn_log`, `learn_session`, `user_note`, `user_settings`, `user_profile` (object, chỉ push).
Trả `{server_time, results: {bảng: {accepted, skipped, rejected: [{id, reason}]}}}`; dòng bị từ chối không chặn dòng khác.
Luật gộp: progress / note / setting bản `client_updated_at` mới hơn thắng (progress còn xét `last_reviewed_at`); review_log chỉ thêm;
day_learn_log lấy max từng bộ đếm; session ghi đè. Push `day_learn_log` thì server tính lại XP / streak.

`GET /api/sync/pull/?tables=a,b&limit=500&cursors={"bảng":"<con trỏ>"}` (cursors là JSON):
trả `{server_time, tables: {bảng: {items, has_more, cursor}}, user_profile}`. Còn `has_more` thì gọi lại với `cursor` mới
(lưu con trỏ từng bảng, vd `system_configs.sync_cursor`). Dòng `is_deleted: true` → app xoá theo. Mặc định không kéo `review_log`.
Bảng pull: như push (trừ `user_profile`) + `user_collection` (danh sách bộ đã tải, chỉ pull; nội dung bộ lấy qua bundle).

Setting app nên dùng: `day_start_hour`, `first_review_hours`, `swipe_min_level`, `swipe_retention`, `request_retention`,
`study_methods`, `cluster_size`, `method_weights`, `interest_tags` (danh sách id tag, mục 8)... (xem `flashwise/docs/learning-design.md`).

---

## 7. Bộ từ, học offline, phiên học

**Xem / tải bộ**
- `GET /api/collections/?lang&q&official=true|false&mine=true&tag=id1,id2&ordering=popular|newest|-like_count|-downloaded_count|-view_count`
  → mỗi bộ `{id, name, description, image_url, senses_count, is_official, language_code, like_count, downloaded_count, view_count, tags, created_at}`.
- `GET /api/collections/{id}/?user_lang=vi` chi tiết + `senses` (mỗi nghĩa có `gloss` = nghĩa ngắn theo `user_lang`, để xem trước bộ không cần tải), `author_id` (id hồ sơ tác giả, để report), `version`, `pending_words`, `invalid_words`. Mỗi lần xem cộng `view_count` (1 lần / người / ngày).
- `GET /api/collections/{id}/versions/` lịch sử phát hành + like từng version + `liked_versions`.
- `POST/DELETE /api/collections/{id}/like/` `{version?}` (mặc định version hiện tại; DELETE không gửi version = bỏ hết) → `{liked, liked_versions, like_count, version, version_like_count}`.
- `POST /api/user-collections/{collection_id}/download/` → `{id}` = id UserCollection (đã tải rồi: 200 cùng id).

**Gói học offline**
`GET /api/user-collections/{user_collection_id}/bundle/?user_lang&etag` → `{etag, unchanged, user_collection, collection, items, words, senses, sense_metadata, pending, banned}` (đúng cột SQLite).
- Gửi `etag` lần trước: không đổi thì chỉ `{etag, unchanged: true}`.
- `pending`: id nghĩa đang được dịch sang `user_lang` → tải lại sau (etag sẽ đổi).
- `banned`: id nghĩa bị admin ban → xoá khỏi SQLite. Bộ bị ban: 403 `collection_banned` → xoá / ẩn bộ.

**Bộ cá nhân** (chủ bộ / người đã tải; dòng nháp của mình luôn thắng bản phát hành)
- Tạo: `POST /api/collections/` `{name, description, tags, language_code, senses: [{id}]}`; hoặc từ danh sách từ: `POST /api/collections/bulk-create/` `{name, description, lang, user_lang, words}` (từ chưa có được AI tạo dần, vào `pending_words`; giới hạn 500 từ mới / ngày).
- `{id}` dưới đây là id UserCollection; vài API cũ đọc thêm `user_collection_id` trong body (gửi cả hai cho chắc).
- `POST user-collections/{id}/add-sense/` `{user_collection_id, sense_id}`; `POST user-collections/{id}/add-senses/` `{sense_ids}`; `POST user-collections/{id}/add-words/` `{words, user_lang}` (chỉ tác giả).
- `POST user-collections/update-sense/` `{user_collection_id, collection_item_id, delta, metadata?, image?, audio?}`; `POST user-collections/{id}/delete-sense/` `{user_collection_id, collection_item_id}`; `POST user-collections/{id}/reorder/` `{items: [{item_id, display_order}]}`.
- `POST user-collections/{id}/upload/` `{user_collection_id, title?, desc?, image?}`: phát hành. Tác giả: luôn đặt bộ công khai (cả phát hành lại sau khi thu hồi); có nghĩa đổi thì version + 1, không thì giữ version và chỉ cập nhật tên / mô tả / ảnh (`changed: false`); bộ chưa từng phát hành mà không có nghĩa nào: 400 `no_changes`. Người khác: tạo bộ remix mới (không có thay đổi: 400 `no_changes`). Bộ remix ghi nguồn: `original_id`, `previous_id`, `parents`, `metadatas[0].remixed_from`; bộ gốc tăng `remix_count`.
- Remix (người khác sửa bộ đã tải): sửa như tác giả (`delete-sense`, `add-senses`, `add-words`), thay đổi là dòng nháp chỉ mình thấy; `reorder` chỉ đổi được dòng nháp của mình. `add-words` của người không phải tác giả chỉ thêm từ đã có nghĩa chính thức, từ còn lại trả trong `unavailable` (không đẩy AI, không đụng `pending_words` của bộ chung). Bấm `upload` thì server tạo bộ remix mới và chuyển `user_collection` của mình sang bộ đó (cùng id, `collection_id` mới): app gọi lại `GET user-collections/` rồi bundle để thấy.
- Bundle `items` gồm cả dòng xoá nháp của chính user (`released=false, is_deleted=true`) để app đếm thay đổi chưa phát hành; app bỏ dòng `is_deleted` khi hiển thị.
- `POST user-collections/{id}/unpublish/`: tác giả thu hồi bộ đã phát hành (`released=false`, giữ nội dung và version). Người chưa tải không thấy / tải được nữa, người đã tải học tiếp. Phát hành lại = `upload` (không cần thay đổi). Không phải tác giả hoặc bộ chính thức: 403; chưa phát hành: 409 `not_released`.
- Dịch lười (`ai/translation.py`): tải bundle KHÔNG đẩy việc dịch; `pending` = id nghĩa chưa có bản dịch sang `user_lang`, `user_collection.translation_missing` = số nghĩa đó (app hiện "đã dịch x / n"). Việc dịch chỉ được đẩy khi chạm tới nghĩa: `learn/new`, `learn/review`, get-word, `GET ai-senses/{id}/`, hoặc dịch trước cụm sắp học bằng `POST ai-senses/translate/` `{sense_ids (≤30), user_lang}` → `{ready, pending}` (hỏi lại sau vài giây / tải lại bundle). `GET collections/{id}/?user_lang` trả thêm `translation: {language, total, missing}`.
- Dịch nền (`redis_trans_lazy`, `ai/jobs.queue_lazy_translation`) — TẮT mặc định, bật bằng `LAZY_TRANS_ENABLED=true` khi có kinh phí AI: khi tải bundle hoặc xem chi tiết bộ (`?user_lang`) mà ngôn ngữ của bộ và của user đều thuộc `POPULAR_LANGUAGES` (mặc định en,vi,ja,zh,ko,es,fr), phần thiếu bản dịch được xếp vào hàng đợi nền. Worker (`consume_lazy`) chỉ lấy việc khi `redis_word / redis_collection_word / redis_image / redis_trans` rỗng và không việc gấp nào đang chạy, từng việc một, cách nhau `LAZY_TRANS_INTERVAL` giây (20); hàng đợi tối đa `LAZY_TRANS_MAX_QUEUE` (5000), mỗi (từ, nguồn, đích) chỉ xếp 1 lần / `LAZY_LOCK_SECONDS` (1 ngày). Cùng dạng job với `redis_trans` (thêm `lazy: true`). Cần restart worker sau khi đổi cờ.
- Nghĩa user sửa (delta): phần sửa (definition / usage / translations trong delta) chỉ có ngôn ngữ user viết, người ngôn ngữ khác xem thì được dịch và bản dịch ghi vào `delta` của chính nghĩa riêng (job `target: 'delta'`), không đụng contents của nghĩa gốc. Khoá chống đẩy trùng nay theo (từ, ngôn ngữ nguồn, ngôn ngữ đích).
- Điểm xếp hạng bộ (`Collection.score`, `core/scores.py`): thích, tải, remix, xem (log), +300 bộ chính thức, -50 bộ dưới 5 nghĩa, uy tín tác giả nhẹ.
- Ảnh bìa: `POST user-collections/{id}/cover/` `{image}` → `{image_url}`. `image` = khoá file đã tải lên qua `POST media/` (hoặc URL ảnh ngoài), `""` = gỡ (về ảnh tự động: app lấy ảnh từ nghĩa trong bộ). Chỉ tác giả (403); bộ đã phát hành thì đổi ảnh qua `upload` (409 `collection_released`). Bundle: `user_collection` có thêm `is_author`, `is_released` (chỉ lưu ở máy) để app biết có hiện nút đổi ảnh bìa không.
- `GET user-collections/{id}/updates/` có bản mới không; `POST user-collections/{id}/update-version/` nâng lên bản mới (trả `conflicts`).
- `GET user-collections/containing/?sense_id`: bộ nào của mình đang chứa nghĩa này.

**Chọn thẻ + xác nhận phiên** (app online dùng; offline app tự chọn bằng cùng luật rồi sync)
- `GET /api/learn/new/?collection_id&user_lang&count&mode=normal&exclude_words=id1,id2` → `{new: [thẻ], new_available, words, senses, sense_metadata, pending}`.
- `GET /api/learn/review/?user_lang&mode=normal|quick&count&collection_id?&exclude_words=id1,id2` → `{due, due_count, due_total, day_ends_at, words, senses, ...}`.
- Không 2 nghĩa cùng 1 từ trong 1 lượt (cả học mới và ôn: giữ thẻ ôn có khả năng nhớ thấp nhất, nghĩa còn lại để lượt sau). `exclude_words` = `word_id` đã có trong phiên (UUID, ngăn cách bằng dấu phẩy) để một phiên gộp từ mới và ôn tập không lặp từ; sai định dạng thì 400.
- `POST /api/learn/new/complete/`, `POST /api/learn/review/complete/` body `{mode, session: {id, start_at, end_at, time, collection_id, methods}, cards: [{sense_id, original_sense_id, log_id, rating (1-4) | known: true, steps: [...]}]}` → `{duplicate, session, progress: [dòng user_sense_progress], rejected}`. Gửi lại cùng `session.id` không áp 2 lần.

---

## 8. Khám phá, chủ đề, thống kê

- `GET /api/tags/?lang&topics=true&user_lang` → `[{id, name, language_code, is_topic, collection_count}]`. `topics=true`: chủ đề cho màn khởi động ("Chọn chủ đề quan tâm"); tên theo `user_lang`. Admin tạo tag trong Django admin (bảng Tag, bật `isSystemTag`, điền `names`).
- Lưu chủ đề đã chọn: setting `interest_tags` = danh sách id tag (đi qua sync). Chưa đăng nhập / chưa sync: truyền thẳng `tags=` khi gọi gợi ý.
- `GET /api/collections/suggested/?lang&limit&tags` → `{interest_tags, results: [bộ + matched_tags]}`: bộ trùng chủ đề quan tâm trước, rồi theo chất lượng; bỏ bộ đã tải / của mình.
- `GET /api/me/uploads/` → panel màn Upload: `{collection_count, view_count, like_count, downloaded_count, collections: [...]}`.
- Home (mục tiêu, số thẻ cần ôn, biểu đồ, streak): app tự tính từ SQLite, không có API riêng.

---

## 9. File ảnh / audio

- `POST /api/media/` (multipart: `file`, `kind` = `image` | `audio`) → 201 `{id, kind, key, url, content_type, size, width, height}`.
  Ảnh ≤5MB (thu nhỏ cạnh dài 1280, đổi WebP); audio ≤2MB: mp3 / m4a / aac / ogg / wav / webm / caf. 50 file / người / ngày.
  Lỗi: 400 `invalid_kind` / `invalid_file` / `file_missing`, 413 `file_too_large`, 429 `quota_exceeded`, 403 `user_banned`.
- App lưu `key` (gửi kèm `audio` / `image` khi sửa nghĩa, `image` khi upload bộ); hiển thị bằng `url` hoặc `media_base_url + key`.
- `GET /api/media/?kind` file của mình; `DELETE /api/media/{id}/`.

---

## 10. Report, góp ý

- `POST /api/reports/` `{target_type: word|sense|collection|user|quiz, target_id, reason}` (user: `target_id` = `author_id` / id hồ sơ). Đối tượng không tồn tại / không thấy: 404. Gửi lại cùng đối tượng = cập nhật (200 `updated: true`).
- `POST /api/feedback/` `{message (≤5000), type?, target_id?}` (20 / ngày, user bị ban vẫn gửi được để khiếu nại).
- Admin (không dùng trong app thường): `reports/queue/`, `reports/resolve/`, `moderation/ban|unban/`, `ai-senses/{id}/promote/`, `ai-words/{id}/regenerate/`.

---

## 11. Việc phía app còn phải làm (backend đã sẵn)

- Gọi `device-login` lúc mở app; `services/axios.ts` đã tự gọi `/refresh-token` khi 401. Bỏ IP ghi cứng, chuyển `fetch` trong `app/screens/Word/Detail/[value].tsx` sang `serverInstance`.
- Worker sync: đọc bảng `sync_queue` → `sync/push`; `sync/pull` theo con trỏ từng bảng.
- Lấy `media_base_url` từ app-config để hiện ảnh / phát audio.
- Màn đăng nhập (Google / Apple / email) theo `auth/providers.configured`; luồng nâng cấp tài khoản khách.
- Màn khởi động: `tags?topics=true` + lưu `interest_tags`. Discover: `collections/suggested`, `collections?tag&ordering`. Upload: `me/uploads`.
- Xử lý `errorCode`: `user_banned`, `collection_banned`, `login_required`, `token_revoked`, `word_generate_throttled`, `quota_exceeded`.
- Bundle: xoá theo `banned`, tải lại khi `pending` khác rỗng.
