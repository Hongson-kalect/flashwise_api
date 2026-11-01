# Việc còn làm: API dữ liệu cốt lõi (word - sense - collection)

File tạm, ghi từ buổi phân tích ngày 2026-11-01. Xong việc nào thì gạch / xoá dòng đó.

## A. Bắt buộc (chưa làm thì cơ chế mô tả chưa đúng)

1. [ ] **get-word kèm nghĩa của user** (cơ chế 2)
   - Hiện `load_word` chỉ lấy `is_official=True`, cache Redis dùng chung cho mọi người.
   - Đọc cache dùng chung rồi, nếu user đã đăng nhập, ghép thêm nghĩa của họ cho từ đó.
   - Nghĩa riêng có `original_id` trùng nghĩa chính thức thì THAY nghĩa đó; nghĩa riêng tạo mới thì THÊM, đặt lên đầu.
   - Không đưa nghĩa riêng vào cache dùng chung.
2. [ ] **Xoá nghĩa của mình** (cơ chế 2, 4)
   - `destroy` của `ai-senses` hiện chỉ admin gọi được.
   - Chưa freeze: soft delete. Đã freeze: không xoá (bộ từ đang trỏ tới sẽ gãy).
   - Cần quyết định: user có được ẩn nghĩa chính thức với riêng mình không (dòng tombstone theo user).
3. [ ] **Nghĩa đã upload phải xem được** (cơ chế 4)
   - `ai-senses` chỉ cho xem nghĩa chính thức + nghĩa của mình, nên nghĩa trong bộ đã tải: `retrieve` 404, `add-sense` sang bộ khác 404, `ai-senses/update-sense` 404 (sửa qua `user-collections/update-sense` thì được).
   - Nới điều kiện: nghĩa đã freeze và nằm trong bộ đã phát hành thì ai cũng xem được.
4. [ ] **Cập nhật bộ đã tải lên version mới** (cơ chế 5)
   - `UserCollection.version` đứng yên ở version lúc tải.
   - `GET user-collections/{id}/updates` (version mới nhất + changelog), `POST user-collections/{id}/update-version`.
   - Xử lý xung đột: user có nháp đè lên từ mà tác giả vừa sửa (nháp vẫn thắng hay báo xung đột).
5. [ ] **Upload chỉ tăng version khi nghĩa thật sự đổi** (cơ chế 5)
   - So `sense_id` từng dòng nháp với dòng đã phát hành mới nhất; không khác thì trả "No changes", không tạo version.

## B. Nên có

6. [ ] `GET ai-words/search?q&lang`: gợi ý từ đã có khi gõ (pg_trgm, hiện chưa dùng chỗ nào).
7. [ ] `GET ai-senses/mine?word_id`: nghĩa riêng của mình (`list` hiện trả mọi nghĩa chính thức, không lọc theo từ).
8. [ ] `POST user-collections/{id}/reorder` `[{item_id, display_order}]`.
9. [ ] `POST user-collections/{id}/add-senses` (nhiều nghĩa) và `add-words` (từ chưa có, vào hàng chờ như bulk-create) cho bộ đã tồn tại.
10. [ ] Tìm / lọc bộ công khai: `collections/?lang&q&ordering=like_count|downloaded_count|-created_at`.
11. [ ] `GET collections/{id}/versions`: lịch sử version từ `metadatas`.
12. [ ] Nghĩa này đang nằm trong bộ nào của tôi (nút "đã thêm" ở màn hình từ).

## C. Admin (từ chỉ do AI tạo)

13. [ ] `POST ai-words/{id}/regenerate`: tạo lại từ sai / thiếu nghĩa (hiện chỉ tạo lại khi FAILED hoặc treo).
14. [ ] Mở lại từ REJECTED nhầm; duyệt `Report` cho nghĩa bị báo sai.
15. [ ] `DELETE ai-words/clear` xoá sạch từ, nghĩa VÀ bộ từ: giới hạn chỉ chạy khi `DEBUG`.

## Lưu ý thiết kế

- `PATCH collections/{id}` sửa thẳng tên / mô tả bộ đã công khai, không qua version: nên chỉ đổi qua `upload` (như `metadatas`).
- Tác giả xoá bộ đã phát hành thì người đã tải mất bộ: nên chỉ ẩn khỏi danh sách công khai.
- `user_collection` và `collection_items` chưa có trong `sync/push` / `sync/pull`; phần học từ vựng sẽ cần.

## Từ phân tích luồng get-word (chưa sửa)

- [ ] Cache ERROR bị gia hạn TTL mỗi lần đọc nên trả 500 mãi khi còn người tra; body 500 lộ dữ liệu cache.
- [ ] Worker chết giữa chừng mất job (`BLPOP` không xác nhận), chỉ có mốc stale 600s cứu.
- [ ] Draft lỗi giữa stream (đã ra vài nghĩa) vẫn được lưu COMPLETED với nghĩa dở dang.
- [ ] `lower()` làm mất chữ hoa có nghĩa (danh từ tiếng Đức, `US` / `us`).
- [ ] Ảnh: `image_preview` là đường dẫn tương đối, chỉ phục vụ khi DEBUG; Pixabay ~100 request/phút/key, seed cần giới hạn hoặc tắt ảnh; chưa lấy bù ảnh cho từ cũ.
