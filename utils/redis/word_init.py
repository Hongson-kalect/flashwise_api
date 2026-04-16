import redis
from redis.commands.json.path import Path
from utils.utils.uuidv7 import generate_uuid7

class WordCacheManager:
    def __init__(self):
        self.r = redis.Redis(host='redis', port=6379, db=1, decode_responses=True)

    def cache_word_init(self, language_code, word_val, user_language_code):
        key = f"word:{language_code}:{word_val}"
        
        initial_data = {
            "word": {"id": str(generate_uuid7()), "value": word_val, "language_code": language_code},
            "senses": {}, 
            "images": {},
            "translates": [user_language_code],
            "status": "PROCESSING"
        }

        try:
            # 1. Thử tạo mới với NX
            is_created = self.r.json().set(key, Path.root_path(), initial_data, nx=True)
            
            if is_created:
                # Chỉ người tạo mới được quyền set expire
                self.r.expire(key, 180) # Thời gian tồn tại của key là 3 phút
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
        
    def cache_word(self, language_code, word_val, data):
        key = f"word:{language_code}:{word_val}"
        
        redis_data = {
            "word": data, 
            "status": "CACHED"
        }
        self.r.json().set(key, Path.root_path(), redis_data)
        
    def cache_word_set_cache(self, language_code, word_val, data):
        # data: {sense_id:{contents full data}}
        key = f"word:{language_code}:{word_val}"
        self.r.json().set(key, Path.root_path(), data, nx=True)
        
    def cache_word_set_status(self, language_code, word_val, status):
        key = f"word:{language_code}:{word_val}"
        self.r.json().set(key, Path(".status"), status)

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

        print("exist key", self.r.exists(key))
        
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

    def cache_word_clear_specific(self, language_code, word_val):
        """1. Xóa sạch cache của một từ cụ thể dựa trên word và language."""
        key = f"word:{language_code}:{word_val}"
        try:
            # Lệnh delete trả về số lượng key đã xóa (1 nếu thành công, 0 nếu không tìm thấy)
            result = self.r.delete(key)
            return result > 0
        except Exception as e:
            print(f"Redis Delete Error: {e}")
            return False

    def cache_word_clear_all(self):
        """2. Xóa sạch sành sanh toàn bộ dữ liệu trong database hiện tại."""
        try:
            # flushdb() chỉ xóa các key trong DB mà bạn đang kết nối (thường là DB 0)
            # Nếu muốn xóa mọi DB trên server thì dùng flushall()
            return self.r.flushall()
        except Exception as e:
            print(f"Redis Flush Error: {e}")
            return False