import uuid

def flatten_ids(data):
    """
    Đi sâu vào cấu trúc lồng nhau (dict, list) để lấy tất cả ID.
    Trả về một set để loại bỏ các ID trùng lặp (nếu có).
    """
    ids = set()

    if isinstance(data, dict):
        for value in data.values():
            # Đệ quy sâu vào value
            ids.update(flatten_ids(value))
    elif isinstance(data, list):
        for item in data:
            # Đệ quy sâu vào từng phần tử trong list
            ids.update(flatten_ids(item))
    elif isinstance(data, (str, uuid.UUID)):
        # Nếu là string hoặc UUID và không phải rỗng thì thêm vào set
        if data:
            ids.add(str(data))

    return list(ids)

def flatten_ids_by_langs(data, target_langs: list):
    """
    Trích xuất ID chỉ từ các ngôn ngữ được chỉ định.
    target_langs: e.g., ['en', 'vi']
    """
    ids = set()

    if isinstance(data, dict):
        # Kiểm tra xem dict này có phải là một "ngăn chứa ngôn ngữ" không
        # Nếu có các key như 'en', 'vi'... ta chỉ lấy những key nằm trong target_langs
        for lang_key, value in data.items():
            if lang_key in target_langs:
                if isinstance(value, str) and value:
                    ids.add(value)
                elif isinstance(value, list): # Đề phòng trường hợp một lang có list ID
                    ids.update([i for i in value if isinstance(i, str)])
            else:
                # Nếu không phải là key ngôn ngữ, tiếp tục đệ quy sâu xuống 
                # (để xử lý các tầng như 'definition', 'examples')
                ids.update(flatten_ids_by_langs(value, target_langs))
                
    elif isinstance(data, list):
        # Duyệt qua các phần tử trong mảng (ví dụ: mảng examples)
        for item in data:
            ids.update(flatten_ids_by_langs(item, target_langs))

    return list(ids)