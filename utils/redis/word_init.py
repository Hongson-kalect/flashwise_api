import time

from redis.commands.json.path import Path
from utils.redis.client import get_cache_client
from utils.utils.uuidv7 import generate_uuid7

TTL = 300  # 5 phút


def _has_value(node):
    if isinstance(node, dict):
        return bool(node.get('value'))
    return bool(node)


def compute_languages(senses, language_code):
    """
    Các ngôn ngữ mà MỌI nghĩa đều đã có đủ nội dung (definition, usage, translations, từng example).
    Lưu cùng cache để get-word biết user_lang đã đủ bản dịch mà không phải duyệt lại toàn bộ nội dung.
    Nội dung gốc rỗng thì không cần dịch nên không bị tính là thiếu.
    """
    final = None

    def intersect(langs):
        nonlocal final
        final = set(langs) if final is None else final & set(langs)

    for sense in senses:
        contents = sense.get('contents') or {}
        if isinstance(contents, str):
            import json
            contents = json.loads(contents)

        for key in ('definition', 'translations'):  # bắt buộc phải có ở mọi nghĩa
            node = contents.get(key)
            intersect(node.keys() if isinstance(node, dict) else ())

        usage = contents.get('usage')
        if isinstance(usage, dict) and _has_value(usage.get(language_code)):
            intersect(usage.keys())

        for example in contents.get('examples') or []:
            if isinstance(example, dict) and _has_value(example.get(language_code)):
                intersect(example.keys())

    return sorted((final or set()) | {language_code})


class WordCacheManager:
    def __init__(self):
        # Client dùng chung của process (decode_responses=True để nhận string/dict sạch từ RedisJSON)
        self.r = get_cache_client()

    def _key(self, language_code, word_val):
        return f"word:{language_code}:{word_val.lower().strip()}"

    def _pipe_expire(self, pipe, key):
        pipe.expire(key, TTL)

    # ================= INIT =================
    def cache_word_init(self, language_code, word_val, user_language_code):
        key = self._key(language_code, word_val)

        # Cấu trúc lưu trữ lý tưởng cho việc map dữ liệu theo ID ở Server và Client
        initial_data = {
            "word": {
                "id": str(generate_uuid7()),
                "value": word_val,
                "language_code": language_code
            },
            "senses": {},      # Lưu dạng { sense_id: { image: "...", contents: "...", metadata: "..." ,... } }
            "langs": [user_language_code],
            "status": "PROCESSING",
            "stage": "draft", # draft: bước 1 đang sinh + dịch; refine: bước 2 đang chuẩn hoá. Nội dung nháp KHÔNG vào cache, chỉ bản cuối (cache_word)
            "started_at": time.time(),  # get-word dùng để phát hiện job chết (key bị đọc liên tục nên TTL trượt không tự hết hạn)
        }

        try:
            pipe = self.r.pipeline()
            pipe.json().set(key, Path.root_path(), initial_data, nx=True)
            self._pipe_expire(pipe, key)

            is_created, _ = pipe.execute()

            if is_created:
                return True, initial_data

            # Sliding TTL khi có người thứ 2, 3 cùng read/query tiến độ giữa chừng
            pipe = self.r.pipeline()
            pipe.json().get(key)
            self._pipe_expire(pipe, key)
            current_data, _ = pipe.execute()

            return False, current_data or initial_data

        except Exception as e:
            print(f"Redis Init Error: {e}")
            return False, initial_data

    # ================= FINAL CACHE =================
    def cache_word(self, language_code, word_id, word_val, data=[]):
        key = self._key(language_code, word_val)

        # 2. CHUẨN HÓA ĐẦU VÀO: Biến thành dict chuẩn dạng {sense_id: sense}
        if isinstance(data, list):
            # Convert list thành dict chuẩn theo ý bạn
            senses_dict = {sense.get('id'): sense for sense in data if isinstance(sense, dict)}
        elif isinstance(data, dict):
            # Nếu đã là dict sẵn rồi thì dùng luôn
            senses_dict = data
        else:
            # Phòng trường hợp data lỗi không phải list/dict
            senses_dict = {}

        languages = compute_languages(senses_dict.values(), language_code)

        redis_data = {
            "word": {
                "id": str(word_id),
                "value": word_val,
                "language_code": language_code
            },
            "senses": senses_dict,
            "languages": languages, # user_lang nằm trong đây thì get-word trả luôn, không cần dò thiếu bản dịch
            "status": "CACHED",
        }

        pipe = self.r.pipeline()
        pipe.json().set(key, Path.root_path(), redis_data)
        self._pipe_expire(pipe, key)
        pipe.execute()

    # ================= UPDATE PARTIAL (Ghi cuốn chiếu O(1)) =================
    def cache_word_set_status(self, language_code, word_val, status):
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        pipe.json().set(key, Path(".status"), status)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_set_stage(self, language_code, word_val, stage):
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        pipe.json().set(key, Path(".stage"), stage)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_set_word(self, language_code, word_val, word_data):
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        pipe.json().set(key, Path(".word"), word_data)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_add_sense(self, language_code, word_val, sense_id, sense_node_data):
        """
        Khởi tạo hoặc cập nhật định nghĩa gốc cho một sense_id cụ thể.
        Ghi trực tiếp vào node .senses.<sense_id>
        """
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        # Đặt trực tiếp vào địa chỉ định danh của sense_id
        pipe.json().set(key, Path(f".senses.{sense_id}"), sense_node_data)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_remove_sense(self, language_code, word_val, sense_id):
        """Bỏ 1 nghĩa khỏi cache (bước chuẩn hoá loại nghĩa trùng / sai). Key hết hạn rồi thì bỏ qua."""
        key = self._key(language_code, word_val)
        try:
            self.r.json().delete(key, Path(f".senses.{sense_id}"))
        except Exception as e:
            print(f"Redis Remove Sense Error: {e}")

    def cache_word_add_metadata(self, language_code, word_val, sense_id, metadata):
        """
        Cập nhật cuốn chiếu trường image cho đúng sense_id mà không làm mất text hay dịch
        """
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        # Chọc thẳng vào thuộc tính image nằm sâu bên trong sense_id cụ thể
        pipe.json().set(key, Path(f".senses.{sense_id}.metadata"), metadata)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_add_image(self, language_code, word_val, sense_id, image_url):
        """
        Cập nhật cuốn chiếu trường image cho đúng sense_id mà không làm mất text hay dịch
        """
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        # Chọc thẳng vào thuộc tính image nằm sâu bên trong sense_id cụ thể
        pipe.json().set(key, Path(f".senses.{sense_id}.image"), image_url)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_add_sense_translate(self, language_code, word_val, sense_id, translate_data):
        """
        Cập nhật cuốn chiếu trường dịch nghĩa (translate) cho đúng sense_id
        """
        key = self._key(language_code, word_val)

        pipe = self.r.pipeline()
        # Chọc thẳng vào thuộc tính translate nằm sâu bên trong sense_id cụ thể
        pipe.json().set(key, Path(f".senses.{sense_id}.contents"), translate_data)
        self._pipe_expire(pipe, key)
        pipe.execute()

    def cache_word_add_translate(self, language_code, word_val, user_language_code):
        key = self._key(language_code, word_val)
        try:
            pipe = self.r.pipeline()
            pipe.json().arrappend(key, Path(".langs"), user_language_code)
            self._pipe_expire(pipe, key)
            pipe.execute()
            return True
        except Exception:
            return False

    # ================= GET =================
    def cache_word_get_data(self, language_code, word_val):
        key = self._key(language_code, word_val)

        try:
            pipe = self.r.pipeline()
            pipe.json().get(key)
            self._pipe_expire(pipe, key)
            result, _ = pipe.execute()
            return result  # Trả về toàn bộ cấu trúc lồng nhau sạch sẽ dưới dạng Python Dict
        except Exception as e:
            print(f"Redis Get Error: {e}")
            return None

    # ================= DELETE =================
    def cache_word_clear_specific(self, language_code, word_val):
        key = self._key(language_code, word_val)
        try:
            return self.r.delete(key) > 0
        except Exception as e:
            print(f"Redis Delete Error: {e}")
            return False

    def cache_word_clear_all(self):
        try:
            return self.r.flushdb()
        except Exception as e:
            print(f"Redis Flush Error: {e}")
            return False