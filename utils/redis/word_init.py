import redis
from redis.commands.json.path import Path

class WordCacheManager:
    def __init__(self):
        self.r = redis.Redis(host='localhost', port=6379, decode_responses=True)

    def cache_word_init(self, language_code, word_val, word_id):
        key = f"word:{language_code}:{word_val}"
        # Lệnh set với nx=True sẽ trả về True nếu tạo mới, False nếu key đã có
        # Chúng ta thêm trường "status" để báo hiệu đang render
        initial_data = {
            "word": {"id": word_id, "value": word_val},
            "senses": {}, 
            "images": {},
            "status": "generating" # <--- Trạng thái quan trọng
        }
        
        # Chỉ SET nếu chưa có ai SET trước đó
        is_created = self.r.json().set(key, Path.root_path(), initial_data, nx=True)
        
        if is_created:
            self.r.expire(key, 86400)

        return is_created
        #     return "LOCKED_BY_ME" # Bạn là người đầu tiên, hãy gọi AI đi!
        # else:
        #     return "ALREADY_EXISTS" # Có người đang làm rồi, chỉ việc ngồi đợi thôi.

    def cache_word_add_sense(self, language_code, word_val, sense_id, sense_data):
        key = f"word:{language_code}:{word_val}"
        # Gán trực tiếp vào ID, nếu trùng sẽ tự update
        self.r.json().set(key, Path(f".senses.{sense_id}"), sense_data)

    def cache_word_add_image(self, language_code, word_val, sense_id, image_url):
        key = f"word:{language_code}:{word_val}"
        self.r.json().set(key, Path(f".images.{sense_id}"), image_url)

    def cache_word_get_data(self, language_code, word_val):
        """Lấy toàn bộ dữ liệu cache của một từ."""
        key = f"word:{language_code}:{word_val}"
        try:
            return self.r.json().get(key)
        except Exception as e:
            print(f"Redis Get Error: {e}")
            return None