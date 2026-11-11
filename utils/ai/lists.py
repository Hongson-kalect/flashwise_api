"""Làm sạch danh sách chuỗi do AI trả về (tags, từ đồng nghĩa, bản dịch...): bỏ mục trùng (không phân biệt hoa thường / khoảng trắng) và cắt còn tối đa `limit` mục."""

MAX_ITEMS = 5


def dedupe(items, limit=MAX_ITEMS):
    """Giữ thứ tự, bỏ mục rỗng hoặc trùng. Mục có thể là chuỗi hoặc {'value': chuỗi}; mục không phải chuỗi giữ nguyên."""
    if not isinstance(items, list):
        return items
    seen, out = set(), []
    for item in items:
        text = item.get('value') if isinstance(item, dict) else item
        if isinstance(text, str):
            key = ' '.join(text.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out[:limit] if limit else out
