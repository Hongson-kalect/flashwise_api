import os
import pandas as pd
from core.models import Language, Word, WordInfo
from django.db import transaction
from django.contrib.auth import get_user_model

User = get_user_model()

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

def import_words_from_excel(file_name='language_en_19k.xlsx', sheet_name='common (2)', test_rows=0):
    """
    Import dữ liệu từ Excel vào Word + WordInfo
    test_rows: nếu >0 sẽ import số dòng đầu tiên để thử
    """
    df = excel_reader(file_name, sheet_name)
    if test_rows > 0:
        df = df.head(test_rows)

    lang = Language.objects.get(code='en')
    user = User.objects.get(username='hongson')

    # Lấy title đã tồn tại trong DB để tránh query nhiều lần
    existing_titles = set(
        Word.objects.filter(lang_code=lang).values_list('title', flat=True)
    )

    # Tạo WordInfo
    word_infos = []
    rows_for_import = []

    for index, row in df.iterrows():
        title = str(row['Word']).strip()
        if not title:
            print(f"⚠️ Skipping existing word: {title} {index}")
            continue

        # word_infos.append(WordInfo(
        #     usage=str(row['Usage']).replace(', N/A', ''),
        #     origin=str(row['Origin2']).replace(', N/A', ''),
        #     remember_tip=str(row['Mnemonic']).replace(', N/A', ''),
        #     story=str(row['InterestingInfo']).replace(', N/A', ''),
        #     tip=str(row['Collocations']).replace(', N/A', ''),
        #     pro_tip=str(row['Common Phrases']).replace(', N/A', ''),
        # ))
        # rows_for_import.append(row)  # chỉ lưu row thực sự được import

    # print(f"⏳ Bulk creating {len(word_infos)} WordInfo objects...")
    # batch_size = 500
    # with transaction.atomic():
    #     for i in range(0, len(word_infos), batch_size):
    #         WordInfo.objects.bulk_create(word_infos[i:i+batch_size])
    #         print(f"✅ WordInfo batch {i}-{i+batch_size} done")

    # # Bulk tạo Word bằng zip → tránh out of range
    # words = []
    # for row, word_info in zip(rows_for_import, word_infos):
    #     words.append(Word(
    #         title=str(row['Word']).strip(),
    #         pronunciation=row['IPA'],
    #         type=[t.strip() for t in str(row['POS']).replace(';', '/').split('/') if t.strip()],
    #         meaning=row['Definition'],
    #         example=row['Example Sentence'],
    #         level=row['CEFR'],
    #         note=f"Topic: {row['Topic']} | Register: {row['Register']}",
    #         tags=[t.strip() for t in str(row['Tags']).replace(';', ',').split(',') if t.strip()],
    #         wordInfo=word_info,
    #         lang_code=lang,
    #         request_by=user,
    #     ))

    # print(f"⏳ Bulk creating {len(words)} Word objects...")
    # with transaction.atomic():
    #     for i in range(0, len(words), batch_size):
    #         Word.objects.bulk_create(words[i:i+batch_size])
    #         print(f"✅ Word batch {i}-{i+batch_size} done")

    # print(f"🎉 Finished importing {len(words)} words successfully!")

if __name__ == "__main__":
    # Chạy thử với 10 dòng đầu tiên để test
    import_words_from_excel(test_rows=10)
