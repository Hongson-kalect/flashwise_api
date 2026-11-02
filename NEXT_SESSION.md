# Tóm tắt để mở phiên mới (cập nhật 2026-11-01)

Đọc file này + `CLAUDE.md` (backend) + `D:\Code\flashwise\docs\learning-design.md` (thiết kế phần học) trước khi làm tiếp.

## Trạng thái code
- Nhánh `refactor/models-review` (backend), **chưa push**. Các commit trong ngày: lấy ảnh Pixabay, chuẩn hoá từ, snapshot từ (`fixtures/ai_words.json`, 19 từ / 51 nghĩa: nạp lại bằng `manage.py ai_words_snapshot import` sau khi xoá DB), nghĩa riêng của user, version bộ từ, tiện ích bộ từ, tìm từ + admin, lỗi get-word, trường dữ liệu phần học. 200 test qua trong Docker (`fwtest`), DB local đã migrate tới `interact 0009`, `tracking 0005`.
- App `D:\Code\flashwise`: đã sửa schema SQLite (`userSenseProgress`, `learnSession`, `userSetting`, bảng mới `reviewLog`, `index.ts`) + thêm `docs/learning-design.md` + dòng trỏ trong `CLAUDE.md`. **Chưa commit** bên app.

## Đã xong phần dữ liệu cốt lõi (word - sense - collection)
CRUD đủ cho: tra từ (AI tạo), nghĩa riêng / ẩn nghĩa, bộ từ nháp / phát hành / version / remix, thêm nghĩa / từ, sắp xếp, tìm từ. Việc còn lại: `TODO_CORE_API.md` (`lower()` mất chữ hoa có nghĩa, worker chết mất job, ảnh cần host / CDN, sync `user_collection` / `collection_items`, dấu nháy cong). Danh sách từ cho autocomplete để lúc làm seed (file theo ngôn ngữ + version, ja / zh cần cột cách đọc).

## Phần học từ vựng: đã chốt
- Nội dung cấp cao (mẹo, văn hoá, nguồn gốc, ngữ pháp, câu hỏi) do user / seeder gõ tay, không để AI sinh; thiếu thì quay về kiểu thẻ cơ bản.
- Ba phương pháp chính: **học cụm** (4–10 từ, mỗi từ 4–8 bước từ nhận ra → nhớ lại → nói / viết), **vuốt** (cấp ≥ 3, phải lật rồi mới vuốt, Quên / Khó chèn lại giữa bộ), **giọng nói** (nhận dạng trên máy, chấm theo cách đọc). Chi tiết + phương pháp phụ: `learning-design.md`.
- Ôn trước, học mới sau (`new_word_first=false`).

## Thay đổi DB đã làm (backend + SQLite, tên cột khớp nhau)
| Bảng | Thay đổi |
|---|---|
| `user_sense_progress` | `level` / `max_level` = cấp thẻ 0–7, mặc định 0 (trước là 1–4 New/Learning/Review/Mastered); thêm `scheduled_days`, `learning_steps` (đủ trường Card của ts-fsrs) |
| `review_log` | thêm `is_fsrs` (1 dòng FSRS / thẻ / phiên, bước trong phiên = 0), `level`, `hint_count`, `attempts`; `mode` dài 40 dạng `learn:cloze`. App có bảng này lần đầu |
| `learn_session` | thêm `status`, `collection_id`, `methods` (JSON), `new_count`, `review_count`, `correct_count`, `wrong_count`, `xp_earned`; type mới `learn / review / voice / speed / mixed`. App thêm `snapshot` (chỉ ở máy, để học tiếp phiên dở) |
| `user_settings` | key mới (seed ở app): `study_methods`, `cluster_size`, `cluster_steps`, `swipe_min_level`, `voice_min_level`, `hearable`, `talkable`, `learning_direction`, `request_retention` |

Sync push / pull (`tracking/sync.py`) đã nhận / trả các trường mới. Push session không gửi `status` thì server mặc định `completed`.

## API cho phần học
- **Đã có, dùng được ngay**: `POST /api/sync/push/`, `GET /api/sync/pull/` cho progress, review_log, learn_session, day_learn_log, user_settings, user_note. App tự chọn thẻ, chấm, tính FSRS (ts-fsrs) rồi đẩy lên.
- **Luồng học đã chốt ngày 2026-11-02**: `D:\Code\flashwise\docs\learning-design.md` mục 5 (độ khó ban đầu 7 phần phiên / 3 phần toàn server, bước theo cấp, ôn cùng ngày, vuốt `request_retention` 0,93, không giới hạn thẻ / ngày, "Đã biết" lên cấp 5) và mục 6 (danh sách việc backend / app).
- **Cần làm, theo thứ tự**:
  1. ~~Gói học offline `GET user-collections/{id}/bundle` + pull `user_collection`~~ (xong 2026-11-02; `collection_items` đi theo bundle, không sync từng dòng).
  1b. ~~`AISense.global_difficulty` / `learner_count` + seed key setting ở app~~ (xong 2026-11-02). Tiếp theo phía app: `fsrsService`, `gradeMapper`, `levelPolicy`, `sessionBuilder` (learning-design.md mục 6).
  2. Cho admin / seeder nhập nội dung cấp cao: quy ước key trong `AISense.contents` (`pro_tip`, `cultural_note`, `etymology`, `grammar_note`, `explanation`, đa ngôn ngữ như definition), kiểm tra `update-sense` / Django admin cho phép các key này.
  3. AI đánh dấu vị trí từ trong câu ví dụ (cho bài điền chỗ trống với từ biến đổi `runs`, `ran`).
  4. Sau: tối ưu tham số FSRS riêng từng user từ `review_log` (`is_fsrs=1`), chấm phát âm trả phí (VIP), thống kê phương pháp theo `mode`.

## Chưa chốt (hỏi user trước khi làm)
- Các con số tạm (trọng số độ khó theo số người học, luật cấp, bước ôn `6h`) chỉnh khi test app thật.

## Lệnh hay dùng
- Test backend: skill `backend-verify` (hoặc `$TEMP/fw/runtests.sh`); commit giữ CRLF/LF: `$TEMP/fw/commit.sh`.
- Venv local: `C:/Users/mouse/.fwvenv`; Docker project `fwtest`.
