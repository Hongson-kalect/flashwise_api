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
