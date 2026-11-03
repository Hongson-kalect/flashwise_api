-- Dữ liệu mẫu cho API GET /api/ai-senses/community/ : nghĩa tuỳ chỉnh của người khác cho từ "table" (en).
-- Chạy:  docker compose exec -T db psql -U postgres -d flashwise -v ON_ERROR_STOP=1 -f - < fixtures/community_senses_table.sql
-- Cần từ "table" đã có (tra 1 lần qua get-word, hoặc ai_words_snapshot import). Chạy lại được nhiều lần (xoá bản cũ theo id cố định).
-- Dọn hẳn: chạy phần "-- XOÁ" ở cuối file.
--
-- Kịch bản (nghĩa gốc chính thức: danh từ đầu tiên của "table"):
--   A  seed_lan    (uy tín 80)  like 42, delta: định nghĩa + 2 ví dụ + usage (đã verified)      -> hạng 1
--   B  seed_minh   (uy tín 20)  like 9,  delta: chỉ 3 ví dụ (định nghĩa kế thừa từ nghĩa gốc)    -> hạng 2
--   C  seed_newbie (uy tín 0)   like 0,  nghĩa riêng khác hẳn (bảng số liệu), không có original -> hạng 3
--   D  seed_lan    nghĩa bị đánh dấu xấu (is_offensive)                                         -> KHÔNG hiện
--   E  seed_minh   nghĩa chưa phát hành (không nằm trong bộ đã phát hành)                       -> KHÔNG hiện
--   F  seed_minh   nghĩa chưa đóng băng (is_frozen = false)                                     -> KHÔNG hiện
--   2 bản tách từ A (previous = A) -> adopted_count của A = 2
BEGIN;

-- XOÁ bản cũ (theo id cố định) để chạy lại được
DELETE FROM collection_items WHERE collection_id = '01a1a000-0000-7000-8000-0000000000c1';
DELETE FROM collection WHERE id = '01a1a000-0000-7000-8000-0000000000c1';
DELETE FROM user_interaction WHERE target_id IN (SELECT id FROM ai_sense WHERE id::text LIKE '01a1a000-0000-7000-8000-0000000000%');
DELETE FROM ai_sense WHERE id::text LIKE '01a1a000-0000-7000-8000-0000000000a%' OR id::text LIKE '01a1a000-0000-7000-8000-0000000000b%';
DELETE FROM sense_metadata WHERE id::text LIKE '01a1a000-0000-7000-8000-0000000000d%';

-- Người dùng mẫu (mật khẩu không dùng được: "!")
INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, is_staff, is_active, date_joined)
SELECT '!', false, u, '', '', u || '@example.test', false, true, now()
FROM (VALUES ('seed_lan'), ('seed_minh'), ('seed_newbie')) AS t(u)
ON CONFLICT (username) DO NOTHING;

INSERT INTO user_profile (id, created_at, updated_at, full_name, avatar_url, gender, country_code, native_language,
                          learning_languages, level, total_xp, current_streak, max_streak, tier, time_zone, is_active,
                          report_accepted, report_rejected, reputation, user_id)
SELECT p.id::uuid, now(), now(), p.name, '', '', 'VN', 'vi', '[]'::jsonb, 1, 0, 0, 0, 'guest', 'Asia/Ho_Chi_Minh', true, 0, 0, p.rep,
       (SELECT id FROM auth_user WHERE username = p.username)
FROM (VALUES
  ('01a1a000-0000-7000-8000-0000000000e1', 'Lan Nguyen',  'seed_lan',    80),
  ('01a1a000-0000-7000-8000-0000000000e2', 'Minh Tran',   'seed_minh',   20),
  ('01a1a000-0000-7000-8000-0000000000e3', 'Newbie',      'seed_newbie',  0)
) AS p(id, name, username, rep)
WHERE NOT EXISTS (SELECT 1 FROM user_profile WHERE user_id = (SELECT id FROM auth_user WHERE username = p.username));
UPDATE user_profile SET reputation = v.rep
FROM (VALUES ('seed_lan', 80), ('seed_minh', 20), ('seed_newbie', 0)) AS v(username, rep)
WHERE user_profile.user_id = (SELECT id FROM auth_user WHERE username = v.username);

-- Metadata riêng của từng nghĩa
INSERT INTO sense_metadata (id, created_at, updated_at, is_active, advanced, tags, image_keywords, image_metadata)
VALUES
 ('01a1a000-0000-7000-8000-0000000000d1', now(), now(), true,
  '{"synonyms": ["desk", "counter"], "collocations": ["kitchen table", "set the table"]}', '["furniture", "daily life"]', '["wooden table"]', '{}'),
 ('01a1a000-0000-7000-8000-0000000000d2', now(), now(), true,
  '{"synonyms": ["board"], "collocations": ["table manners"]}', '["furniture"]', '["dining table"]', '{}'),
 ('01a1a000-0000-7000-8000-0000000000d3', now(), now(), true,
  '{"synonyms": ["chart", "spreadsheet"]}', '["data"]', '["spreadsheet table"]', '{}');

-- A: delta (nội dung = nghĩa gốc + delta)
INSERT INTO ai_sense (id, created_at, updated_at, is_active, word_id, word_value, language_code, pos, level, register, ipas,
                      origins, preview, contents, delta, voted, devoted, views, is_offensive, is_frozen, versions, is_official,
                      is_ai_created, score, like_count, main_count, global_forget_count, global_remember_count, global_difficulty,
                      learner_count, quality_status, ai_pass, ai_review, metadata_id, original_id, created_by_id)
SELECT s.id::uuid, now() - s.age, now(), true, w.id, 'table', 'en', 'noun', 'A1', ARRAY['neutral'], '[]'::jsonb,
       '{}'::jsonb, '{}'::jsonb, s.contents::jsonb, s.delta::jsonb, 0, 0, 0, s.offensive, s.frozen, 1, false,
       false, 0, s.likes, 0, 0, 0, 0, 0, s.quality, 1, '{}'::jsonb, s.meta::uuid,
       s.original::uuid, (SELECT id FROM auth_user WHERE username = s.author)
FROM ai_word w,
(VALUES
 -- A
 ('01a1a000-0000-7000-8000-0000000000a1', 'seed_lan', interval '5 days', 42, 'verified', false, true,
  '{}',
  '{"definition": {"en": {"value": "A piece of furniture with a flat top and legs, used for eating, working or putting things on."},
                   "vi": {"value": "Đồ nội thất có mặt phẳng và chân, dùng để ăn, làm việc hoặc đặt đồ."}},
    "usage": {"en": {"value": "Often used with at / on: at the table, on the table."},
              "vi": {"value": "Thường đi với at / on: at the table, on the table."}},
    "examples": [{"en": {"value": "Please set the table for dinner."}, "vi": {"value": "Làm ơn dọn bàn cho bữa tối."}},
                 {"en": {"value": "She left her keys on the table."}, "vi": {"value": "Cô ấy để quên chìa khoá trên bàn."}}]}',
  '01a1a000-0000-7000-8000-0000000000d1', '01a19f53-7989-74e9-b8fb-de031f53984c'),
 -- B
 ('01a1a000-0000-7000-8000-0000000000a2', 'seed_minh', interval '3 days', 9, 'draft', false, true,
  '{}',
  '{"examples": [{"en": {"value": "We eat breakfast at the kitchen table."}, "vi": {"value": "Chúng tôi ăn sáng ở bàn bếp."}},
                 {"en": {"value": "The table is made of oak."}, "vi": {"value": "Chiếc bàn làm bằng gỗ sồi."}},
                 {"en": {"value": "Wipe the table after lunch."}, "vi": {"value": "Lau bàn sau bữa trưa."}}]}',
  '01a1a000-0000-7000-8000-0000000000d2', '01a19f53-7989-74e9-b8fb-de031f53984c'),
 -- C (nghĩa riêng, không có original)
 ('01a1a000-0000-7000-8000-0000000000a3', 'seed_newbie', interval '1 day', 0, 'draft', false, true,
  '{"definition": {"en": {"value": "A set of facts or numbers arranged in rows and columns."},
                   "vi": {"value": "Tập hợp dữ kiện hoặc số liệu sắp xếp theo hàng và cột."}},
    "translations": {"vi": ["bảng", "bảng số liệu"]},
    "examples": [{"en": {"value": "The table shows sales by month."}, "vi": {"value": "Bảng này cho thấy doanh số theo tháng."}}]}',
  '{}',
  '01a1a000-0000-7000-8000-0000000000d3', NULL),
 -- D: bị đánh dấu xấu
 ('01a1a000-0000-7000-8000-0000000000b1', 'seed_lan', interval '4 days', 100, 'draft', true, true,
  '{}', '{"definition": {"en": {"value": "[offensive sample]"}}}', NULL, '01a19f53-7989-74e9-b8fb-de031f53984c'),
 -- E: chưa phát hành (không có trong collection_items)
 ('01a1a000-0000-7000-8000-0000000000b2', 'seed_minh', interval '2 days', 50, 'draft', false, true,
  '{}', '{"definition": {"en": {"value": "[unpublished sample]"}}}', NULL, '01a19f53-7989-74e9-b8fb-de031f53984c'),
 -- F: chưa đóng băng
 ('01a1a000-0000-7000-8000-0000000000b3', 'seed_minh', interval '2 days', 50, 'draft', false, false,
  '{}', '{"definition": {"en": {"value": "[not frozen sample]"}}}', NULL, '01a19f53-7989-74e9-b8fb-de031f53984c')
) AS s(id, author, age, likes, quality, offensive, frozen, contents, delta, meta, original)
WHERE w.id = '01a19f53-6fcd-7865-860e-04f6cd7bf194';

-- 2 bản tách từ A (adopted_count của A = 2): previous = A
INSERT INTO ai_sense (id, created_at, updated_at, is_active, word_id, word_value, language_code, pos, register, ipas, origins, preview,
                      contents, delta, voted, devoted, views, is_frozen, versions, is_official, is_ai_created, score, like_count,
                      main_count, global_forget_count, global_remember_count, global_difficulty, learner_count, quality_status,
                      ai_pass, ai_review, original_id, previous_id, created_by_id)
SELECT v.id::uuid, now(), now(), true, '01a19f53-6fcd-7865-860e-04f6cd7bf194', 'table', 'en', 'noun', ARRAY[]::varchar[], '[]'::jsonb,
       '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 0, 0, 0, false, 1, false, false, 0, 0, 0, 0, 0, 0, 0, 'draft', 1, '{}'::jsonb,
       '01a19f53-7989-74e9-b8fb-de031f53984c', '01a1a000-0000-7000-8000-0000000000a1', (SELECT id FROM auth_user WHERE username = v.author)
FROM (VALUES ('01a1a000-0000-7000-8000-0000000000a9', 'seed_minh'), ('01a1a000-0000-7000-8000-0000000000aa', 'seed_newbie')) AS v(id, author);

-- Bộ từ đã phát hành chứa A, B, C, D, F (E để ngoài)
INSERT INTO collection (id, created_at, updated_at, name, description, item_count, released, metadatas, like_count, view_count,
                        downloaded_count, is_uploaded, language_code, is_active, is_official, version, invalid_words, pending_words,
                        parents, is_frozen, created_by_id)
VALUES ('01a1a000-0000-7000-8000-0000000000c1', now(), now(), 'Đồ dùng trong nhà (mẫu)', 'Bộ mẫu để thử gợi ý nghĩa cộng đồng', 5, true,
        ARRAY[]::jsonb[], 0, 0, 0, true, 'en', true, false, 1, ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::uuid[], true,
        (SELECT id FROM auth_user WHERE username = 'seed_lan'));

INSERT INTO collection_items (id, display_order, is_active, is_base, released, version, created_at, updated_at, parent, collection_id,
                              created_by_id, sense_id)
SELECT gen_random_uuid(), row_number() OVER (), true, false, true, 1, now(), now(), ARRAY[]::uuid[],
       '01a1a000-0000-7000-8000-0000000000c1', (SELECT id FROM auth_user WHERE username = 'seed_lan'), s::uuid
FROM unnest(ARRAY['01a1a000-0000-7000-8000-0000000000a1', '01a1a000-0000-7000-8000-0000000000a2', '01a1a000-0000-7000-8000-0000000000a3',
                  '01a1a000-0000-7000-8000-0000000000b1', '01a1a000-0000-7000-8000-0000000000b3']) AS s;

-- Người dùng hệ thống id=1 đã thích nghĩa A (thử cờ `liked`)
INSERT INTO user_interaction (id, created_at, updated_at, is_active, interaction_type, user_id, status, target_id, target_type, version)
VALUES (gen_random_uuid(), now(), now(), true, 'like', 1, true, '01a1a000-0000-7000-8000-0000000000a1', 'sense', 1);

COMMIT;

-- XOÁ (dọn hẳn dữ liệu mẫu):
-- DELETE FROM user_interaction WHERE target_id::text LIKE '01a1a000-0000-7000-8000-0000000000%';
-- DELETE FROM collection_items WHERE collection_id = '01a1a000-0000-7000-8000-0000000000c1';
-- DELETE FROM collection WHERE id = '01a1a000-0000-7000-8000-0000000000c1';
-- DELETE FROM ai_sense WHERE id::text LIKE '01a1a000-0000-7000-8000-0000000000a%' OR id::text LIKE '01a1a000-0000-7000-8000-0000000000b%';
-- DELETE FROM sense_metadata WHERE id::text LIKE '01a1a000-0000-7000-8000-0000000000d%';
-- DELETE FROM user_profile WHERE user_id IN (SELECT id FROM auth_user WHERE username LIKE 'seed\_%');
-- DELETE FROM auth_user WHERE username LIKE 'seed\_%';
