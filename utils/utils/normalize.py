import re


def normalize(word: str) -> str:
    if not word:
        return ""

    # 1. Đưa về lowercase
    word = word.lower()

    # 2. Loại bỏ các ký tự đặc biệt
    # Giữ lại: chữ cái, số, khoảng trắng và các ký tự: ' - , .
    # Biểu thức chính quy: [^...] nghĩa là "không phải những ký tự này"
    # \w bao gồm chữ cái và số (hỗ trợ tốt cả tiếng Việt có dấu và chữ CJK)
    word = re.sub(r"[^\w\s'\-\,\.]", "", word)

    # 3. Xoá khoảng trắng thừa bên trong và 2 bên
    # \s+ khớp với một hoặc nhiều khoảng trắng, tab, xuống dòng
    return " ".join(word.split())


def normalize_lookup(word: str) -> str:
    """
    Khoá chuẩn của 1 từ khi tra (get-word) và khi worker tạo từ: viết thường + gộp khoảng trắng, KHÔNG bỏ ký tự.
    Phải khớp đúng normalizeWord của app (trim, gộp khoảng trắng, lowercase) vì app tự băm md5 chuỗi này làm room socket;
    view, cache key, bản ghi AIWord, prompt AI và room đều dùng chung chuỗi này. Bỏ ký tự như normalize() sẽ làm
    'c++' thành 'c' ở worker trong khi view vẫn chờ key 'c++'.
    """
    return " ".join((word or "").split()).lower()


def is_lookup_word(word: str) -> bool:
    """Chuỗi đã qua normalize_lookup có ít nhất 1 chữ / số (vd '!!!' thì không: khỏi tốn 1 lần gọi AI để bị từ chối)."""
    return bool(word) and len(word) <= 100 and re.search(r"\w", word) is not None
