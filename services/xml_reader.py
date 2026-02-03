import bz2
import gzip
import json

file_path = "D:/Temp/en-wiki.bz2"

# with bz2.open(file_path, "rt", encoding="utf-8") as f:
#     for i in range(10):  # đọc 10 dòng đầu tiên
#         line = f.readline()
#         print(line.strip())

def read_xml_file(lines=100):
    with bz2.open(file_path, "rt", encoding="utf-8") as f:
        for i in range(200):  # đọc 10 dòng đầu tiên
            line = f.readline()
            print(line.strip())

    # with open(file_path, "r", encoding="utf-8") as f:
    #     for i in range(lines):  # đọc 10 dòng đầu tiên
    #         line = f.readline()
    #         print(line.strip())

def read_jsonl_file(lines=10):
    jsonl_path = "D:/Temp/raw-wiki-data.jsonl"
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for i in range(lines):  # đọc 10 dòng đầu tiên
            line = f.readline()
            print(line.strip())

def read_language_lines(max_lines=10):
    # jsonl_path = "D:/Temp/en-wiki.jsonl"
    jsonl_path = "D:/Temp/ja-wiki.jsonl"
    count = 0

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # bỏ qua dòng lỗi

            # if data.get("lang_code") == language_code:
                # print(data.get("word"))
            print(data)
            print("-----------------------------")
            count += 1

            if count >= max_lines:
                break

support_lang =["en","es","fr","de","ja","zh","ko","ru","it","pt","ar","nl","pl","sv","no","da","fi","el","tr","cs","hu","vi","th","hi","id","he",]
def read_file_lines(max_lines=10):
    # jsonl_path = "D:/Temp/en-wiki.jsonl"
    jsonl_path = "D:/Temp/en-wiki.gz"
    count = 0

    with gzip.open(jsonl_path, "rt", encoding="utf-8") as f:
        for i,line in enumerate(f):
            if(i > 0 and i % 10000 == 0):
                print(f"Processed {i} lines, count so far: {count} ")

            if(i > 0 and i % 1000000 == 0):
                break
            try:
                data = json.loads(line)
                lang = data.get("lang_code")
                if lang in support_lang:
                    count += 1
            except:
                continue


        print(f"Total lines: {count}")
