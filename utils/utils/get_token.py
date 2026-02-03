
import csv
import datetime
import gzip
from itertools import count
import json
import os
from tracemalloc import start
from unittest import result
from django.contrib.auth import get_user_model
from django.utils import timezone
import re
from numpy import concat
from rest_framework import viewsets, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import api_view, permission_classes, authentication_classes,action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from core.models import Defination, Example, ExampleTranslate, Language, WordForm, WordInfo
from core.models.Word import Word
from core.serializers.Word import WordSerializer
from utils.models.Ruby import Ruby
from .jwt import generate_tokens_for_user, is_refresh_token_valid
from uuid6 import uuid7

from django.db import models
from django.db.models import Prefetch

User = get_user_model()

@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def dev_token(request):
    user = User.objects.get(username = 'hongson')
    
    tokens = create_new_token(user)
    return token_response(tokens)
            
           
def create_new_token(user):
    tokens = generate_tokens_for_user(user)
    return tokens

def token_response(tokens):
    return Response({
        **tokens,
        # "user_info": s.UserProfileSerializer(user_info).data
    })

# @api_view(["GET"])
# @permission_classes([])
# @authentication_classes([])
# def push_csv(request):

# lang = "zh" is the last language use to get other words and definations
lang = "ja"

@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def jmdict(request):
    languages = list(Language.objects.all())
    ja_language = next((lang for lang in languages if lang.code == "ja"), None)

    count = {
        "mapping": 0,
        "existing": 0,
        "new": 0,
        'new_defination': 0,
        're_defination': 0,
        'new_example': 0,
        're_example': 0,
        'new_example_trans': 0,
        're_example_trans': 0,
    }
    # for i in range(50):
    # file_path = f"D:/Temp/jmdict-{i}.jsonl"
    note = 'JMdict'

    kanji = Ruby.objects.all()
    

    for i in range(51):
        # if(count["mapping"]>100):
        #         break

        file_path = f"D:/Temp/JMdict_en/term_bank_{i+2}.json"
        values = json.loads(open(file_path, 'r', encoding='utf-8').read())
        for value in values:

            if(count["mapping"] % 1000 == 0):
                print(f"Processed {count['mapping']} lines")
            # if(count["mapping"]>100):
            #     break
            count["mapping"] += 1
            word = value[0]
            
            ruby_string = value[0]
            existing_ruby =[]
            reading = value[1]
            raw_pos = value[2]
            parser = parse_pos_string(raw_pos)
            pos = parser.get('pos', None)
            if not pos:
                continue
            tags = parser.get('tags', [])

            senses = value[5]
            exist_word = True
            word_obj = Word.objects.filter(value=word, word_info__pos=pos, language_code ='ja').select_related('word_info').first()
            info = None
            if not word_obj:
                count["new"] += 1
                exist_word = False
                word_info = WordInfo.objects.create(
                            # word=word_obj,
                            # tip=notes_arr,
                            pos = pos,
                            tags = tags
                        )
                word_obj = Word.objects.create(
                    value=word,
                    reading=reading,
                    language_code='ja',
                    note=note,
                    language = ja_language,
                    word_info = word_info
                )
            else :
                count["existing"] += 1
                word_info = word_obj.word_info
                if(word_obj.rubys):
                    existing_ruby = word_obj.rubys
            if not isinstance(senses, list):
                continue

            for sense in senses:
                if not isinstance(sense, dict):
                    continue
                contens= sense.get('content', None)
                if not contens:
                    continue

                if not isinstance(contens, list):
                    contens = [contens]

                for content in contens:
                    lang_code = content.get('lang', None)
                    language = next((lang for lang in languages if lang.code == lang_code), None)
                    if not language:
                        continue

                    content_type = content.get('data', None)
                    type = content_type.get('content', None)

                    if(type == "glossary"): # defination
                        definations = content.get('content', [])
                        if not isinstance(definations, list):
                            definations = [definations]

                        value = []
                        for defination in definations:
                            val = defination.get("content",'')
                            ruby_string+=val
                            value.append(val)

                        exist_definations = None
                        if exist_word: 
                            exist_definations = Defination.objects.filter(word=word_obj, language=language).first()
                        if not exist_definations:
                            Defination.objects.create(
                                value=value,
                                word=word_obj,
                                language=language,
                                language_code=language.code,
                            )
                            count["new_defination"] += 1
                        else:
                            count["re_defination"] += 1
                        
                    elif(type == 'examples'):
                        origin_example =None
                        examples = content.get('content', [])
                        if not isinstance(examples, list):
                            examples = [examples]
                        
                        for example in examples:
                            self_lang_code = example.get('lang', lang_code)
                            language = next((lang for lang in languages if lang.code == self_lang_code), None)

                            example_text = example.get('content', None)
                            if not example_text:
                                continue

                            if self_lang_code=='ja':
                                ruby_string += example_text
                                if exist_word:
                                    existing_example = Example.objects.filter(value=example_text, word=word_obj).first()
                                    if existing_example:
                                        origin_example = existing_example
                                        count["re_example"] += 1
                                        continue

                                example =Example.objects.create(
                                    value=example_text,
                                    word=word_obj,
                                    language=language,
                                    language_code=language.code,
                                )
                                count["new_example"] += 1
                                origin_example = example
                            else :
                                if origin_example:
                                    existing_example = ExampleTranslate.objects.filter(value=example_text, word=word_obj, language=language, example=origin_example).first()
                                    if existing_example:
                                        count["re_example_trans"] += 1
                                        continue

                                    ExampleTranslate.objects.create(
                                        value=example_text,
                                        word=word_obj,
                                        language=language,
                                        language_code=language.code,
                                        example=origin_example,
                                    )
                                    count["new_example_trans"] += 1
                    
                    elif(type == 'notes'): # etymology
                        notes_arr=None
                        notes = content.get('content', [])
                        if not isinstance(notes, list):
                            notes = [notes]

                        notes_arr = [note.get("content",None) for note in notes]

                        word_info.tip = notes_arr
                        word_info.save()
                    
                    ruby = extract_kanji(ruby_string)
                    new_ruby = kanji.filter(value__in=ruby).exclude(id__in=existing_ruby)

                    if(len(new_ruby)):
                        new_ruby_ids = [str(ruby.id) for ruby in new_ruby]

                        for id in new_ruby_ids:
                            existing_ruby.append(id)
                        
                        word_obj.rubys = existing_ruby
                        word_obj.save()

    return Response({"status": "completed", "count": count})


@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def add_kanji(request):

    qs = WordInfo.objects.all()

    total = qs.count()
    print(f"Total: {total}")

    count =0
    start_time = timezone.now()
    current_time = start_time
    affected =0
    for info in qs.iterator(chunk_size=1000):
        count+=1
        if(count % 1000 == 0):
            leap = timezone.now() - current_time
            current_time = timezone.now()
            total_leap = timezone.now() - start_time
            print(f"Processed {count} words, affected: {affected}, leap: {leap}, total_leap: {total_leap}")
        ipas = info.ipas
        fixed=[]
        for ipa in ipas:
            data_fixed = {}
            for key, value in ipa.items():
                if(key =='tags'):
                    data_fixed['tags']=value
                else:
                    data_fixed['value']=value

            fixed.append(data_fixed)
        info.ipas = fixed
        info.save()

            

        # if(count>10):
        #     break

    return Response({"status": "completed", "processed": count, "affected": affected})

    # Duyệt JMdict bỏ kiểu forms, dựa vào id item[6] để xác định cùng 1 từ. Nếu id = prev_id -> thêm dữ liệu cho từ trước đó. pos Bắt đầu = số, loại bỏ (nó là stt của sense)
    # 1. Nếu từ tồn tại
    # 1.1 Thêm defination, example, en translate nếu chưa có
    # 1.2 Thêm ruby mới

    # 2. Nếu từ chưa tồn tại
    # 2.1 Thêm word (ruby, tạo id trước, sau mới add row), info, translate, defination, example, example_translate

@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def fix_data(request):
    # jsonl_path = f"D:/Temp/{lang}-extract.jsonl.gz"
    jsonl_path = f"D:/Temp/{lang}-extract.jsonl.gz"
    lines = get_gz_file_line(jsonl_path)
    print(f"Total lines in gz file: {lines}")
    language_path = "D:/Code/flashcardApi/utils/utils/csv/language.csv"
    supported_lang = read_csv_to_dicts(language_path)


    start_time = timezone.now()
    current_time = start_time
    count={
        "word": 0,
        "example": 0,
        "example_translate": 0,
        "definations": 0,
    }

    # defination_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/defination.csv"
    # example_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example.csv"
    # example_translate_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example_translate.csv"
    defination_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/defination.csv"
    example_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example.csv"
    example_translate_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example_translate.csv"

    file_language = next((language for language in supported_lang if language["code"] == lang), None)

    start_time = timezone.now()
    current_time = start_time

    with \
    open(defination_path, "w", newline="", encoding="utf-8") as definationfile, \
    open(example_path, "w", newline="", encoding="utf-8") as examplefile, \
    open(example_translate_path, "w", newline="", encoding="utf-8") as example_translatefile:
        defination_writer = csv.writer(definationfile)
        example_writer = csv.writer(examplefile)
        example_translate_writer = csv.writer(example_translatefile)
        
        #header
        example_writer.writerow(["id","sub_id","value","word","defination_id","bold","lang","language_code","score","roman","bold_roman","ruby","is_active",])
        example_translate_writer.writerow(["id","sub_id","value","word","bold","translate","example","language","language_code","score","roman","bold_roman","ruby","is_active",])
       
        defination_writer.writerow(["id","sub_id", "lang","language_code", "value", "bold","word", "score", "roman", "ruby"])
        
        with gzip.open(jsonl_path, mode="rt", encoding="utf-8") as f:
            for i,line in enumerate(f):
                # if(count["word"] >= 100):
                #     break

                if(i > 0 and i % 1000 == 0):
                        now = timezone.now()
                        elapsed_seconds = (now - start_time).total_seconds()
                        elapsed_2 = (now - current_time).total_seconds()
                        current_time = now
                        print(f"Processed {i}/{lines} lines, {i/lines*100:.2f}%, elapsed: {elapsed_2:.2f}/{elapsed_seconds:.2f}, count: {count.get('word',0)}/{count.get('example',0)}")


                obj = json.loads(line)
                lang_code = next((lang for lang in supported_lang if lang["code"] == obj.get("lang_code", None)), None)
                if(not lang_code):
                    continue # bỏ qua ngôn ngữ không hỗ trợ

                word = obj.get("word", "")
                pos = obj.get("pos", "")
                senses = obj.get("senses", [])
                if(len(senses) == 0):
                    continue

                word_data = Word.objects.filter(
                            value=word, word_info__pos=pos).select_related('word_info').first()
                if(not word_data):
                    continue
                count["word"] += 1

                word_data.is_fixed = True
                word_data.save()

                # word_definitions = list(word_data.definations.all()) 
                word_examples = list(word_data.word_examples.all())
                for sense in senses:
                    definations = sense.get("glosses", [])
                    if(len(definations) == 0):
                        continue
                    
                    def_id = str(uuid7())
                    defination_writer.writerow([
                        def_id, #id,
                        def_id, #sub_id,
                        file_language["id"], #lang,
                        file_language["code"], #language_code,
                        json.dumps(definations), #value,
                        None, #bold,
                        word_data.id, #word,
                        100, #score,
                        None, #roman,
                        None, #ruby,
                    ])
                    count["definations"] += 1

                    examples = sense.get("examples", [])
                    prev_example_text = ''
                    for example in examples:
                        value = example.get("text", "")
                        if(not value or value == prev_example_text):
                            continue
                        roman = example.get("roman", None)
                        bold = example.get("bold_text_offsets", None)
                        roman_bold = example.get("bold_roman_offsets", None)
                        ruby = example.get("ruby", None)
                        example_id = str(uuid7())
                        this_example = next((ex for ex in word_examples if ex.value == value), None)

                        if(this_example):
                            example_id = this_example.id

                        else:
                            example_writer.writerow([
                                example_id, #id,
                                example_id, #sub_id,
                                value, #value,
                                word_data.id, #word,
                                def_id, #defination_id,
                                json.dumps(bold), #bold,
                                lang_code["id"], #lang,
                                lang_code["code"], #language_code,
                                100, #score,
                                roman, #roman,
                                json.dumps(roman_bold), #bold_roman,
                                json.dumps(ruby), #ruby,
                                True, #is_active,
                            ])
                            count["example"] += 1  
                        
                        prev_example_text = value

                        example_translate = example.get("translation", "")
                        if(not example_translate):
                            continue

                        example_translate_id = str(uuid7())
                        bold = example.get("bold_translation_offsets", [])

                        # get roman and ruby if file lang = ja
                        example_translate_writer.writerow([
                            example_translate_id, #id,
                            example_translate_id, #sub_id,
                            example_translate, #value,
                            word_data.id, #word,
                            bold, #bold,l
                            None, #translate,
                            example_id, #example,
                            file_language["id"], #lang,
                            file_language["code"], #language_code,
                            100, #score,
                            None, #roman,
                            None, #bold_roman,
                            None, #ruby,
                            True, #is_active,
                        ])
                        count["example_translate"] += 1  

                        

    return Response({"status": "completed","count": count})

@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def read_word(request):
    read_line = request.query_params.get('line', 100)
    offset = request.query_params.get('offset', 0)
    count = 0
    result = []

    language_path = "D:/Code/flashcardApi/utils/utils/csv/language.csv"
    supported_lang = read_csv_to_dicts(language_path)
    file_language = next((lang for lang in supported_lang if lang["code"] == lang), None)

    # count["languages"] = len(supported_lang)
    # jsonl_path = f"D:/Temp/JMdict.gz"
    jsonl_path = f"D:/Temp/{lang}-extract.jsonl.gz"

    with gzip.open(jsonl_path, mode="rt", encoding="utf-8") as f:
        for i,line in enumerate(f):
            if i < offset:
                continue


            if(i > 0 and i % 1000 == 0):
                print(f"Processed {i} lines, current count: {count}")

            if(count > read_line + offset):
                break

            # if(count >= 10):
            #     break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                senses = obj.get("senses", [])
                word = obj.get("word", "")
                pos = obj.get("pos", "")

                if(len(senses) == 0):
                    continue

                lang_code = next((lang for lang in supported_lang if lang["code"] == obj.get("lang_code", None)), None)
                if(not lang_code or not pos or not word):
                    continue # bỏ qua ngôn ngữ không hỗ trợ
                
                result.append(obj)
                count += 1

            except json.JSONDecodeError:
                continue  # bỏ qua dòng lỗi
    return Response({"status": "completed","count": count, "data": result})

@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def add_bonus_word(request):
    count = {
        "languages": 0,
        "definations": 0,
        "forms": 0,
        "examples": 0,
        "example_translates": 0,
        "word_infos": 0,
        "translates": 0,
        "other_lang": 0,
        "affected": 0,
        "not found": 0,
    }

    language_path = "D:/Code/flashcardApi/utils/utils/csv/language.csv"
    defination_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/defination.csv"
    form_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/form.csv"
    example_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example.csv"
    example_translate_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/example_translate.csv"
    word_info_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/word_info.csv"
    translate_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/translate.csv"
    word_path = f"D:/Code/flashcardApi/utils/utils/csv/{lang}/word.csv"


    supported_lang = read_csv_to_dicts(language_path)
    file_language = next((language for language in supported_lang if language["code"] == lang), None)

    # count["languages"] = len(supported_lang)

    jsonl_path = f"D:/Temp/{lang}-extract.jsonl.gz"

    lines = get_gz_file_line(jsonl_path)
    print(f"Total lines in gz file: {lines}")
    start_time = timezone.now()
    current_time = start_time

    with \
    open(defination_path, "w", newline="", encoding="utf-8") as definationfile, \
    open(form_path, "w", newline="", encoding="utf-8") as formfile, \
    open(example_path, "w", newline="", encoding="utf-8") as examplefile, \
    open(example_translate_path, "w", newline="", encoding="utf-8") as example_translatefile, \
    open(word_info_path, "w", newline="", encoding="utf-8") as word_infofile, \
    open(translate_path, "w", newline="", encoding="utf-8") as translatefile, \
    open(word_path, "w", newline="", encoding="utf-8") as wordfile:
        defination_writer = csv.writer(definationfile)
        form_writer = csv.writer(formfile)
        example_writer = csv.writer(examplefile)
        example_translate_writer = csv.writer(example_translatefile)
        word_info_writer = csv.writer(word_infofile)
        translate_writer = csv.writer(translatefile)
        word_writer = csv.writer(wordfile)
        
        #header
        word_info_writer.writerow(["id", "sub_id","pos","ipas","audios","images","usage","etymology","interesting_info","tip","tags","topics","level",])
        form_writer.writerow(["id","sub_id","value","word","type","roman","ruby",])
        example_writer.writerow(["id","sub_id","value","word","defination_id","bold","lang","language_code","score","roman","bold_roman","ruby","is_active",])
        example_translate_writer.writerow(["id","sub_id","value","word","bold","translate","example","language","language_code","score","roman","bold_roman","ruby","is_active",])
        translate_writer.writerow(["id", "sub_id","value","word","lang","language_code","is_auto","detail","is_active","request_by",])

        defination_writer.writerow(["id","sub_id", "lang","language_code", "value", "bold","word", "score", "roman", "ruby"])
        word_writer.writerow(["id","sub_id","value","language","language_code","synonyms","antonyms","relateds","word_info","note","score","is_active"])

        # Đọc từng dòng JSONL
        with gzip.open(jsonl_path, mode="rt", encoding="utf-8") as f:
            for i,line in enumerate(f):               

                if(i > 0 and i % 1000 == 0):
                    now = timezone.now()
                    elapsed_seconds = (now - start_time).total_seconds()
                    elapsed_2 = (now - current_time).total_seconds()
                    current_time = now
                    print(f"Processed {i}/{lines} lines, {i/lines*100:.2f}%, elapsed: {elapsed_2}/{elapsed_seconds}, count: {count.get('affected',0)}")

                # if(i > 0 and i % 1000 == 0):
                #     break

                # if(count["affected"] >= 100):
                #     break

                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    senses = obj.get("senses", [])
                    word = obj.get("word", "")
                    pos = obj.get("pos", "")

                    if(len(senses) == 0):
                        continue

                    lang_code = next((lang for lang in supported_lang if lang["code"] == obj.get("lang_code", None)), None)
                    if(not lang_code or not pos or not word):
                        count["other_lang"] += 1
                        continue # bỏ qua ngôn ngữ không hỗ trợ

                    # get word + filter with info (to get have same pos), tran + defi
                    word_data = Word.objects.filter(
                        value=word,
                        language_code=lang_code["code"], word_info__pos=pos).select_related('word_info').first()
                    # data = WordSerializer(word_data).data

                    if word_data:
                        word_id = word_data.id
                    else:
                        sounds = obj.get("sounds", [])
                        forms = obj.get("forms", [])

                        # Get word info
                        pos = obj.get("pos", '')
                        ipas = get_ipas_from_sounds(sounds)
                        audios = get_audios_from_sounds(sounds)
                        etymology = obj.get("etymology_text", None)
                        tags = obj.get("categories", [])
                        info_id = str(uuid7())
                        word_info_writer.writerow([
                            info_id, #id,
                            info_id, #sub_id,
                            pos, #pos,
                            json.dumps(ipas), #ipas,
                            json.dumps(audios), #audios,
                            json.dumps([]), #images,
                            None, #usage,
                            etymology, #etymology,
                            None, #interesting_info,
                            None, #tip,
                            json.dumps(tags), #tags,
                            json.dumps([]), #topics,
                            None, #level,
                            ])
                        count["word_infos"] += 1
                        
                        # Get word
                        word_id = str(uuid7())
                        value = obj.get("word", "")
                        synonyms = [synonym.get('word') for synonym in obj.get("synonyms", [])]
                        antonyms = [antonym.get('word') for antonym in obj.get("antonyms", [])]
                        relateds = [related.get('word') for related in obj.get("relateds", [])]
                        word_info =  info_id
                        word_writer.writerow([
                            word_id, #id,
                            word_id, #sub_id,
                            value, #value,
                            lang_code["id"], #language,
                            lang_code["code"], #language_code,
                            json.dumps(synonyms), #synonyms,
                            json.dumps(antonyms), #antonyms,
                            json.dumps(relateds), #relateds,
                            word_info, #word_info,
                            "", #note,
                            100, #score,
                            True, #is_active
                        ])
                        count["not found"] += 1

                        # Get form
                        for form in forms:
                            form_id = str(uuid7())
                            value = form.get("form", "")
                            type = form.get("tags", [])
                            roman = form.get("roman", "")
                            ruby = form.get("ruby", "")
                            form_writer.writerow([
                                form_id, #id,
                                form_id, #sub_id,
                                value, #value,
                                word_id, #word,
                                json.dumps(type), #type,
                                roman, #roman,
                                ruby, #ruby,
                                ])
                            count["forms"] += 1

                    # exist?
                    # 1. add definition with langcode
                    for sense in senses:
                        definations = sense.get("glosses", [])
                        if(len(definations) == 0):
                            continue

                        def_id = str(uuid7())

                        defination_writer.writerow([
                            def_id, #id,
                            def_id, #sub_id,
                            file_language["id"], #lang,
                            file_language["code"], #language_code,
                            json.dumps(definations), #value,
                            None, #bold,
                            word_id, #word,
                            100, #score,
                            None, #roman,
                            None, #ruby,
                            ])
                        count["definations"] += 1
                        # 2. add example
                        word_examples = list(word_data.word_examples.all()) if word_data else []
                        examples = sense.get("examples", [])
                        prev_example_text = ''
                        for example in examples:
                            value = example.get("text", "")
                            if(not value or value == prev_example_text):
                                continue

                            example_id = str(uuid7())
                            bold = example.get("bold_text_offsets", [])
                            roman = example.get("roman", "")
                            bold_roman = example.get("bold_roman_offsets", [])
                            ruby = example.get("ruby", "")

                            this_example = next((ex for ex in word_examples if ex.value == value), None)

                            if(this_example):
                                example_id = this_example.id

                            else:
                                example_writer.writerow([
                                    example_id, #id,
                                    example_id, #sub_id,
                                    value, #value,
                                    word_id, #word,
                                    def_id, #defination_id,
                                    json.dumps(bold), #bold,
                                    lang_code["id"], #lang,
                                    lang_code["code"], #language_code,
                                    100, #score,
                                    roman, #roman,
                                    json.dumps(bold_roman), #bold,
                                    json.dumps(ruby), #ruby,
                                    True, #is_active
                                    ])
                                count["examples"] += 1
                            prev_example_text = value

                            # Get example translation
                            example_translate = example.get("translation", None)
                            if(not example_translate):
                                continue

                            id = str(uuid7())
                            bold = example.get("bold_translation_offsets", [])
                            example_translate_writer.writerow([
                                id, #id,
                                id, #sub_id,
                                example_translate, #value,
                                word_id, #word,
                                json.dumps(bold), #bold,
                                None,
                                example_id, #example,
                                file_language["id"], #lang,
                                file_language["code"], #language_code,
                                100, #score,
                                None, #roman,
                                None, #bold_roman,
                                None, #ruby,
                                True, #is_active
                                ])
                            count["example_translates"] += 1
                    # 3. add trans if not exist: get trans -> compare
                    # old_translates = data.get('translates',[])
                    old_translates = list(word_data.word_translates.all()) if word_data else []
                    old_translate_words = [t.value for t in old_translates]

                    translates = obj.get("translations", [])
                    for translate in translates:
                        value = translate.get("word", "")
                        if(not value or value in old_translate_words):
                            continue
                        translate_lang_code =translate.get("lang_code", translate.get('code',None))
                        translate_lang_code_id = next((lang.get("id", None) for lang in supported_lang if lang["code"] == translate_lang_code), None)

                        id = str(uuid7())
                        translate_writer.writerow([
                            id, #id,
                            id, #sub_id,
                            value, #value,
                            word_id, #word,
                            translate_lang_code_id, #lang,
                            translate_lang_code, #language_code,
                            False, #is_auto,
                            None, #detail,
                            True, #is_active,
                            None, #request_by,
                            ])
                        count["translates"] += 1
                        
                except json.JSONDecodeError:
                    continue  # bỏ qua dòng lỗi

                count["affected"]+= 1
                # break
    return Response({"status": "completed","count": count})
    
@api_view(["GET"])
@permission_classes([])
@authentication_classes([])
def add_word(request):
    # user = User.objects.get(username = 'hongson')

    
    count = {
        "languages": 0,
        "definations": 0,
        "forms": 0,
        "examples": 0,
        "example_translates": 0,
        "word_infos": 0,
        "translates": 0,
        "words": 0,
        "format_errors": 0,
    }
    
    jsonl_path = "D:/Temp/en-wiki.jsonl"
    language_path = "D:/Code/flashcardApi/utils/utils/csv/language.csv"
    defination_path = "D:/Code/flashcardApi/utils/utils/csv/defination.csv"
    form_path = "D:/Code/flashcardApi/utils/utils/csv/form.csv"
    example_path = "D:/Code/flashcardApi/utils/utils/csv/example.csv"
    example_translate_path = "D:/Code/flashcardApi/utils/utils/csv/example_translate.csv"
    word_info_path = "D:/Code/flashcardApi/utils/utils/csv/word_info.csv"
    tag_path = "D:/Code/flashcardApi/utils/utils/csv/tag.csv"
    translate_path = "D:/Code/flashcardApi/utils/utils/csv/translate.csv"
    word_path = "D:/Code/flashcardApi/utils/utils/csv/word.csv"

    supported_lang = read_csv_to_dicts(language_path)
    count["languages"] = len(supported_lang)

    # print(f"Supported languages: {len(supported_lang)}")

    # Nếu file CSV chưa tồn tại, tạo mới với header
    # file_exists = os.path.exists(csv_path)
    with \
    open(defination_path, "w", newline="", encoding="utf-8") as definationfile, \
    open(form_path, "w", newline="", encoding="utf-8") as formfile, \
    open(example_path, "w", newline="", encoding="utf-8") as examplefile, \
    open(example_translate_path, "w", newline="", encoding="utf-8") as example_translatefile, \
    open(word_info_path, "w", newline="", encoding="utf-8") as word_infofile, \
    open(translate_path, "w", newline="", encoding="utf-8") as translatefile, \
    open(word_path, "w", newline="", encoding="utf-8") as wordfile:
        defination_writer = csv.writer(definationfile)
        form_writer = csv.writer(formfile)
        example_writer = csv.writer(examplefile)
        example_translate_writer = csv.writer(example_translatefile)
        word_info_writer = csv.writer(word_infofile)
        translate_writer = csv.writer(translatefile)
        word_writer = csv.writer(wordfile)
        
        #header
        word_info_writer.writerow(["id", "sub_id","pos","ipas","audios","images","usage","etymology","interesting_info","tip","tags","topics","level",])
        form_writer.writerow(["id","sub_id","value","word","type","roman","ruby",])
        example_writer.writerow(["id","sub_id","value","word","bold","lang","language_code","score","roman","bold_roman","ruby","is_active",])
        example_translate_writer.writerow(["id","sub_id","value","word","bold","translate","example","language","language_code","score","roman","bold_roman","ruby","is_active",])
        translate_writer.writerow(["id", "sub_id","value","word","lang","language_code","is_auto","detail","is_active","request_by",])

        defination_writer.writerow(["id","sub_id", "lang","language_code", "value", "bold","word", "score", "roman", "ruby"])
        word_writer.writerow(["id","sub_id","value","language","language_code","synonyms","antonyms","relateds","word_info","note","score","is_active"])

        # Đọc từng dòng JSONL
        with open(jsonl_path, mode="r", encoding="utf-8") as f:
            for i,line in enumerate(f):
                if(i > 0 and i % 10000 == 0):
                    print(f"Processed {i} lines")

                # if(i >= 100):
                #     break
                
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    count["format_errors"] += 1
                    continue  # bỏ qua dòng lỗi

                lang_code = next((lang for lang in supported_lang if lang["code"] == obj.get("lang_code", None)), None)
                # FOR en kakki file
                file_language = next((lang for lang in supported_lang if lang["code"] == "en"), None)

                if(not lang_code):
                    continue # bỏ qua ngôn ngữ không hỗ trợ

                sounds = obj.get("sounds", [])
                senses = obj.get("senses", [])
                forms = obj.get("forms", [])

                # Get word info
                pos = obj.get("pos", '')
                ipas = get_ipas_from_sounds(sounds)
                audios = get_audios_from_sounds(sounds)
                etymology = obj.get("etymology_text", None)
                tags = obj.get("categories", [])
                info_id = str(uuid7())
                word_info_writer.writerow([
                    info_id, #id,
                    info_id, #sub_id,
                    pos, #pos,
                    json.dumps(ipas), #ipas,
                    json.dumps(audios), #audios,
                    json.dumps([]), #images,
                    None, #usage,
                    etymology, #etymology,
                    None, #interesting_info,
                    None, #tip,
                    json.dumps(tags), #tags,
                    json.dumps([]), #topics,
                    None, #level,
                    ])
                count["word_infos"] += 1
                
                # Get word
                word_id = str(uuid7())
                value = obj.get("word", "")
                synonyms = [synonym.get('word') for synonym in obj.get("synonyms", [])]
                antonyms = [antonym.get('word') for antonym in obj.get("antonyms", [])]
                relateds = [related.get('word') for related in obj.get("relateds", [])]
                word_info =  info_id
                word_writer.writerow([
                    word_id, #id,
                    word_id, #sub_id,
                    value, #value,
                    lang_code["id"], #language,
                    lang_code["code"], #language_code,
                    json.dumps(synonyms), #synonyms,
                    json.dumps(antonyms), #antonyms,
                    json.dumps(relateds), #relateds,
                    word_info, #word_info,
                    "", #note,
                    100, #score,
                    True, #is_active
                ])
                count["words"] += 1

                # Get form
                for form in forms:
                    form_id = str(uuid7())
                    value = form.get("form", "")
                    type = form.get("tags", [])
                    roman = form.get("roman", "")
                    ruby = form.get("ruby", "")
                    form_writer.writerow([
                        form_id, #id,
                        form_id, #sub_id,
                        value, #value,
                        word_id, #word,
                        json.dumps(type), #type,
                        roman, #roman,
                        ruby, #ruby,
                        ])
                    count["forms"] += 1
                    
                # Get defination
                for sense in senses:
                     # Chỉ có định nghĩa bằng tiếng của file thay vì tiếng của chính ngôn ngữ đó??
                    definitions = sense.get("glosses", [])
                    for definition in definitions:
                        def_id = str(uuid7())
                        value = definition

                        defination_writer.writerow([
                            def_id, #id,
                            def_id, #sub_id,
                            file_language["id"], #lang,
                            file_language["code"], #language_code,
                            value, #value,
                            None, #bold,
                            word_id, #word,
                            100, #score,
                            None, #roman,
                            None, #ruby,
                            ])
                        count["definations"] += 1

                # Get translate
                translates = obj.get("translations", [])
                for translate in translates:
                    id = str(uuid7())
                    value = translate.get("word", "")
                    if(not value):
                        continue
                    translate_lang_code =translate.get("lang_code", translate.get('code',None))
                    translate_lang_code_id = next((lang.get("id", None) for lang in supported_lang if lang["code"] == translate_lang_code), None)

                    translate_writer.writerow([
                        id, #id,
                        id, #sub_id,
                        value, #value,
                        word_id, #word,
                        translate_lang_code_id, #lang,
                        translate_lang_code, #language_code,
                        False, #is_auto,
                        None, #detail,
                        True, #is_active,
                        None, #request_by,
                        ])
                    count["translates"] += 1
                # Get example
                for sense in senses:
                    examples = sense.get("examples", [])
                    for example in examples:
                        example_id = str(uuid7())
                        value = example.get("text", "")
                        bold = example.get("bold_text_offsets", [])
                        score = example.get("score", 0)
                        roman = example.get("roman", "")
                        bold_roman = example.get("bold_roman_offsets", [])
                        ruby = example.get("ruby", "")
                        example_writer.writerow([
                            example_id, #id,
                            example_id, #sub_id,
                            value, #value,
                            word_id, #word,
                            json.dumps(bold), #bold,
                            lang_code["id"], #lang,
                            lang_code["code"], #language_code,
                            100, #score,
                            roman, #roman,
                            json.dumps(bold_roman), #bold,
                            json.dumps(ruby), #ruby,
                            True, #is_active
                            ])
                        count["examples"] += 1

                        # Get example translation
                        if(file_language):
                                    value = example.get("translation", "")

                                    if(value):
                                        id = str(uuid7())
                                        bold = example.get("bold_translation_offsets", [])
                                        example_translate_writer.writerow([
                                            id, #id,
                                            id, #sub_id,
                                            value, #value,
                                            word_id, #word,
                                            json.dumps(bold), #bold,
                                            None,
                                            example_id, #example,
                                            file_language["id"], #lang,
                                            file_language["code"], #language_code,
                                            100, #score,
                                            None, #roman,
                                            None, #bold_roman,
                                            None, #ruby,
                                            True, #is_active
                                            ])
                                        count["example_translates"] += 1

                #after all, remap all word to sync synonyms, antonyms, relateds with ids instead of values
        return Response({"status": "completed", "count": count})


def read_csv_to_dicts(csv_path):
    rows = []

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)  # tự lấy dòng đầu làm header
        for row in reader:
            rows.append(dict(row))   # chuyển OrderedDict -> dict

    return rows

def get_ipas_from_sounds(sounds):
    ipas = []

    for sound in sounds:
        ipa = sound.get("ipa", None)
        ipa_tags = sound.get("tags", None)
        if ipa :
            ipas.append({
                "value": ipa,
                "tags": ipa_tags
            })
    return ipas

def get_audios_from_sounds(sounds):
    audios = []

    for sound in sounds:
        audio = sound.get("audio", None)

        if audio :
            url =[]
            mp3_url = sound.get("mp3_url", None)
            ogg_url = sound.get("ogg_url", None)
            tags = sound.get("tags", None)
            text = sound.get("text", None)

            if mp3_url:
                url.append({
                    "mp3_url": mp3_url
                })
            if ogg_url:
                url.append({
                    "ogg_url": ogg_url
                })

            audios.append({
                "url": url,
                "tags": tags,
                "text": text,
                "audio": audio,
            })


    return audios
def get_gz_file_line(path):
    with gzip.open(path, mode="rt", encoding="utf-8") as f:
        return sum(1 for _ in f)

def add_defination_helper(senses, word_writer):
    pass
def add_example_translate_helper(word_data, word_writer):
    pass
def add_example_helper(word_data, word_writer):
    pass
def add_form_helper(word_data, word_writer):
    pass
def add_translate_helper(word_data, word_writer):
    pass
def add_word_info_helper(word_data, word_writer):
    pass
def add_word_helper(word_data, word_writer):
    pass

POS_MAP = {
    "n": "noun",
    "pn": "pronoun",
    "adj-i": "i-adjective",
    "adj-na": "na-adjective",
    "n-adj": "na-adjective",
    "v1": "ichidan-verb",
    "v5": "godan-verb",
    "vs": "suru-verb",
    "adv": "adverb",
    "prt": "particle",
    "ctr": "counter",
    "int": "interjection",
    "exp": "expression",
}

LABEL_MAP = {
    "uk": "usually kana",
    "col": "colloquial",
    "euph": "euphemistic",
    "obs": "obsolete",
    "sl": "slang",
    "vulg": "vulgar",
    "pol": "polite",
    "hum": "humble",
    "derog": "derogatory",
    "fam": "familiar",
    "lit": "literary",
}


def parse_pos_string(pos_string: str):
    """
    Parse JMdict POS string like:
    "2 n col uk euph" -> {"pos": "noun", "tags": ["colloquial", "usually kana", "euphemistic"]}
    """

    # Normalize input
    parts = pos_string.strip().split()

    # Remove leading sense index (optional number)
    if parts and parts[0].isdigit():
        parts = parts[1:]

    if not parts:
        return {"pos": None, "tags": []}

    # Extract POS (first non-number entry)
    raw_pos = parts[0]
    pos = POS_MAP.get(raw_pos, raw_pos)  # fallback: keep original if unknown

    # Remaining parts are labels/tags
    raw_labels = parts[1:]
    tags = [LABEL_MAP.get(lbl, lbl) for lbl in raw_labels]

    return {
        "pos": pos,
        "tags": tags,
    }

KANJI_REGEX = r'[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df\U0002a700-\U0002b73f\U0002b740-\U0002b81f\U0002b820-\U0002ceaf\uf900-\ufaff]'

# Regex Kanji Unicode ranges

def extract_kanji(text: str) -> list[str]:
    """
    Trả về danh sách các ký tự Kanji trong chuỗi text
    """
    kanji_chars = []
    for char in text:
        if re.findall(KANJI_REGEX, char):
            kanji_chars.append(char)
    return kanji_chars
