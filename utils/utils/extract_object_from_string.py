def extract_json_fragment(full_text: str, path_key: str, index: int = None):
    # 1. Tìm vị trí của key (ví dụ: "senses")
    start_idx = full_text.find(f'"{path_key}"')
    if start_idx == -1:
        return None

    # 2. Tìm dấu mở mảng [ sau key đó
    array_start = full_text.find("[", start_idx)
    if array_start == -1:
        return None

    # 3. Nếu yêu cầu lấy CẢ ARRAY (index là None)
    if index is None:
        return _extract_balanced_structure(full_text, array_start, "[", "]")

    # 4. Nếu yêu cầu lấy PHẦN TỬ THỨ N
    current_pos = array_start + 1
    found_count = 0
    
    while found_count <= index:
        # Tìm dấu mở ngoặc { của object tiếp theo trong mảng
        obj_start = full_text.find("{", current_pos)
        if obj_start == -1:
            return None
        
        # Trích xuất object cân bằng dấu ngoặc
        obj_string = _extract_balanced_structure(full_text, obj_start, "{", "}")
        
        if obj_string:
            if found_count == index:
                return obj_string
            # Nếu chưa tới index cần tìm, nhảy qua object này để tìm tiếp
            current_pos = obj_start + len(obj_string)
            found_count += 1
        else:
            # Object chưa đóng ngoặc (đang stream dở)
            return None

    return None

def _extract_balanced_structure(text, start_idx, open_char, close_char):
    """Hàm phụ trợ để lấy khối cân bằng ngoặc, xử lý cả string và escape."""
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
