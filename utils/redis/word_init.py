import redis
from redis.commands.json.path import Path
from utils.utils.uuidv7 import generate_uuid7
from flashcardApi import settings

TTL = 300  # 5 phút

class WordCacheManager:
    def __init__(self):
        # Đảm bảo cài đặt decode_responses=True để nhận string/dict sạch từ RedisJSON
        self.r = redis.Redis(host='redis', port=6379, db=1, decode_responses=True)

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
            "senses": {},      # Lưu dạng { sense_id: { definition: "...", image: "...", translate: "..." } }
            "langs": [user_language_code],
            "status": "PROCESSING"
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
    def cache_word(self, language_code, word_val, data):
        key = self._key(language_code, word_val)

        redis_data = {
            "word": data,
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