import redis
from redis.commands.json.path import Path

class WordCacheManager:
    def __init__(self):
        self.r = redis.Redis(host='localhost', port=6379, decode_responses=True)

    def cache_word_init(self, language_code, word_val, user_language_code):
        key = f"word:{language_code}:{word_val}"
        
        initial_data = {
            "word": {"id": "", "value": word_val, "language_code": language_code},
            "senses": {}, 
            "images": {},
            "translates": [user_language_code],
            "status": "generating"
        }

        try:
            # 1. Thử tạo mới với NX
            is_created = self.r.json().set(key, Path.root_path(), initial_data, nx=True)
            
            if is_created:
                # Chỉ người tạo mới được quyền set expire
                self.r.expire(key, 86400)
                return True, initial_data
            
            # 2. Nếu không tạo được (đã tồn tại), lấy dữ liệu hiện có
            current_data = self.r.json().get(key)
            
            # Phòng hờ trường hợp hy hữu: Key vừa tồn tại nhưng lúc GET lại bị biến mất
            if current_data is None:
                # Đệ quy lại một lần hoặc trả về dữ liệu ảo để tránh crash Client
                return False, initial_data 
                
            return False, current_data

        except Exception as e:
            print(f"Redis Init Error: {e}")
            return False, initial_data

    def cache_word_set_word(self, language_code, word_val, word_data):
        key = f"word:{language_code}:{word_val}"
        # Gán trực tiếp vào ID, nếu trùng sẽ tự update
        self.r.json().set(key, Path(f".word"), word_data)
    def cache_word_add_sense(self, language_code, word_val, sense_id, sense_data):
        key = f"word:{language_code}:{word_val}"
        # Gán trực tiếp vào ID, nếu trùng sẽ tự update
        self.r.json().set(key, Path(f".senses.{sense_id}"), sense_data)

    def cache_word_add_image(self, language_code, word_val, sense_id, image_url):
        key = f"word:{language_code}:{word_val}"
        self.r.json().set(key, Path(f".images.{sense_id}"), image_url)
    
    def cache_word_add_translate(self, language_code, word_val, user_language_code):
        key = f"word:{language_code}:{word_val}"
        
        # 1. Lấy danh sách hiện tại
        current_translates = self.r.json().get(key, Path(".translates"))
        
        # 2. Nếu chưa có ngôn ngữ này trong danh sách đợi, thì mới thêm vào
        if current_translates is not None and user_language_code not in current_translates:
            self.r.json().arrappend(key, Path(".translates"), user_language_code)
            return True
        
        return False # Đã tồn tại hoặc lỗi)

    def cache_word_get_data(self, language_code, word_val):
        """Lấy toàn bộ dữ liệu cache của một từ."""
        key = f"word:{language_code}:{word_val}"
        try:
            return self.r.json().get(key)
        except Exception as e:
            print(f"Redis Get Error: {e}")
            return None