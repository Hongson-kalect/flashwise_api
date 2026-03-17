import re
import json

def extract_json_fragment(full_text: str, keyword: str, last_pos: int = 0, start_search_at: int = 0):
    """
    Sử dụng Regex để tìm chính xác Key JSON, tránh bắt nhầm keyword trong chuỗi text.
    """
    effective_pos = max(last_pos, start_search_at)
    
    # 1. TRẠNG THÁI: Tìm cụm Keyword mới (ví dụ sau khi hết một mảng cũ)
    if last_pos == 0 or start_search_at > 0:
        # Regex này tìm: "keyword" theo sau là dấu hai chấm (có thể có khoảng trắng)
        # Cách này loại bỏ 99% trường hợp "senses" nằm trong ví dụ hoặc định nghĩa.
        pattern = re.compile(f'"{re.escape(keyword)}"\s*:')
        match = pattern.search(full_text, effective_pos)
        
        if not match: return None, 0
        
        start_idx = match.end() # Vị trí ngay sau dấu ":"
        
        # Tìm dấu mở mảng '[' sau key
        array_start = full_text.find("[", start_idx)
        if array_start == -1: return None, 0
        
        # Tìm dấu mở object '{' đầu tiên
        obj_start = full_text.find("{", array_start)
    
    # 2. TRẠNG THÁI: Đang duyệt tiếp trong mảng
    else:
        next_obj = full_text.find("{", last_pos)
        next_end_array = full_text.find("]", last_pos)
        
        if next_obj == -1 and next_end_array == -1: return None, last_pos
        
        # Nếu gặp dấu đóng mảng ']' trước -> Chuyển sang tìm Keyword tiếp theo
        if next_end_array != -1 and (next_obj == -1 or next_end_array < next_obj):
            return extract_json_fragment(full_text, keyword, last_pos=0, start_search_at=next_end_array + 1)
        
        obj_start = next_obj

    if obj_start == -1: return None, last_pos

    # 3. TRÍCH XUẤT CÂN BẰNG NGOẶC (Giữ nguyên logic cũ vì nó cực kỳ ổn định)
    obj_string = _extract_balanced_structure(full_text, obj_start, "{", "}")
    
    if obj_string:
        return obj_string, obj_start + len(obj_string)
    
    return None, last_pos

def _extract_balanced_structure(text, start_idx, open_char, close_char):
    count = 0
    is_in_string = False
    escape_char = False
    
    for i in range(start_idx, len(text)):
        char = text[i]
        if char == "\\" and not escape_char:
            escape_char = True
            continue
        if char == '"' and not escape_char:
            is_in_string = not is_in_string
        
        if not is_in_string:
            if char == open_char:
                count += 1
            elif char == close_char:
                count -= 1
            if count == 0:
                return text[start_idx : i + 1]
        escape_char = False
    return None