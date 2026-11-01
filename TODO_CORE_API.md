# Việc còn làm: API dữ liệu cốt lõi (word - sense - collection)

Ghi từ buổi phân tích ngày 2026-11-01. Mục A, B, C, lưu ý thiết kế và 2 lỗi get-word đã làm xong
(các commit d4b2edf .. f2ea793 trên nhánh refactor/models-review). Còn lại:

## Chưa làm

- [ ] `lower()` làm mất chữ hoa có nghĩa (danh từ tiếng Đức, `US` / `us`): phải đổi cùng lúc với `normalizeWord` của app (room socket).
- [ ] Worker chết giữa chừng mất job (`BLPOP` không xác nhận), chỉ có mốc stale 600s cứu.
- [ ] Ảnh: `image_preview` là đường dẫn tương đối, chỉ phục vụ khi DEBUG (cần host / CDN); Pixabay ~100 request/phút/key, seed cần giới hạn hoặc tắt ảnh; chưa lấy bù ảnh cho từ cũ (job `redis_image` {sense_id, keywords} đã có, chưa ai đẩy).
- [ ] Sync `user_collection` / `collection_items` với app (sync push / pull) cho phần học từ vựng.
- [ ] Dấu nháy cong (`don’t` / `don't`) là 2 từ khác nhau, muốn gộp phải sửa cả app.

## Ghi chú hành vi mới (để app làm theo)

- get-word với user đăng nhập trả cả nghĩa riêng: `is_official=false`, `original` = id nghĩa bị thay; nghĩa dạng delta có `contents` của nghĩa gốc, app áp `delta` lên.
- `SENSE_REMOVED` giờ cũng được gửi khi stream draft đứt và worker chuyển sang model khác.
- 500 / 404 của get-word từ cache chỉ còn `value`, `language_code` (500 thêm `retry_after`).
- Các action mới trên user-collections (`updates`, `update-version`, `add-senses`, `add-words`, `reorder`) lấy id bộ từ URL; các action cũ vẫn đọc `user_collection_id` trong body.
