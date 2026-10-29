import os
import pandas as pd


def excel_reader(file_name, sheet_name):
    """
    Đọc file Excel và trả về DataFrame
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'files', file_name)
    print(f"🔍 Reading: {file_path}")

    df = pd.read_excel(file_path, sheet_name=sheet_name)
    df.fillna('', inplace=True)  # thay NaN bằng chuỗi rỗng
    return df


def read_word_list(file_name='language_en_19k.xlsx', sheet_name='common (2)', column='Word', limit=0):
    """
    Lấy danh sách từ (đã bỏ trùng, giữ thứ tự) để đẩy vào hàng đợi tạo trước AIWord.
    Bản import cũ vào bảng Word/WordInfo đã bỏ cùng các bảng đó.
    """
    df = excel_reader(file_name, sheet_name)
    if limit > 0:
        df = df.head(limit)

    seen = set()
    words = []
    for value in df[column]:
        word = str(value).strip().lower()
        if word and word not in seen:
            seen.add(word)
            words.append(word)
    return words
