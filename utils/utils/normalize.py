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


MAX_ENTRY_TOKENS = 12    # tục ngữ dài nhất cỡ này ("a bird in the hand is worth two in the bush")
MAX_ENTRY_CJK_CHARS = 14  # ja / zh không có dấu cách: đếm ký tự
CJK_LANGUAGES = ('ja', 'zh')
_SENTENCE_END = re.compile(r"[.!?。！？]")
_SENTENCE_GAP = re.compile(r"[.!?。！？]\s*\S")


def lookup_entry_problem(word: str, language_code: str):
    """
    Từ điển chỉ chứa từ đơn, cụm từ cố định, thành ngữ, tục ngữ, không chứa câu bất kỳ ai gõ vào.
    Lọc rẻ trước khi tốn lượt gọi AI (AI còn phán quyết thêm ở bước tạo từ): trả 'too_long' / 'sentence' nếu chắc chắn không phải mục từ, ngược lại None.
    Chuỗi đã qua normalize_lookup.
    """
    if (language_code or '').split('-')[0] in CJK_LANGUAGES:
        if len(word) > MAX_ENTRY_CJK_CHARS:
            return 'too_long'
        stripped = word.rstrip('.!?。！？')
        return 'sentence' if _SENTENCE_END.search(stripped) else None
    tokens = word.split()
    if len(tokens) > MAX_ENTRY_TOKENS:
        return 'too_long'
    body = word.rstrip('.!?。！？ ')
    if _SENTENCE_GAP.search(body):  # dấu kết câu rồi lại có chữ: nhiều câu
        return 'sentence'
    if word != body and len(tokens) > 5:  # kết thúc bằng dấu câu và dài: câu hoàn chỉnh
        return 'sentence'
    return None


def is_lookup_word(word: str) -> bool:
    """Chuỗi đã qua normalize_lookup có ít nhất 1 chữ / số (vd '!!!' thì không: khỏi tốn 1 lần gọi AI để bị từ chối)."""
    return bool(word) and len(word) <= 100 and re.search(r"\w", word) is not None
