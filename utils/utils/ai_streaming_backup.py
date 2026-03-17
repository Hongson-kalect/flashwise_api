import asyncio
import json
from google import genai
from django.http.response import StreamingHttpResponse
from google.genai import types
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes,action
from ai import models
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from ai.serializers.AIWord import AIWordSerializer
from core.models.Language import Language
from core.models.Word import Word
from flashcardApi import settings
from django.db import transaction

from django.contrib.auth.models import User
from ai.models.AIWord import AIWord
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AISenseContent import AISenseContent
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.sense_handle import serialize_entries, serialize_senses

# The client gets the API key from the environment variable `GEMINI_API_KEY`
client = genai.Client(api_key=settings.GEMINI_API_KEY)

# @api_view(["GET"])
# @permission_classes([])
# @authentication_classes([])
from asgiref.sync import sync_to_async

# @api_view(["GET"])
# @permission_classes([])
# @authentication_classes([])
async def full_data(data, callback=None):
    yield data
    # await asyncio.sleep(0.01) # Nhả for, giúp event stream gửi dữ liệu ngay lập tức
    if callback:
        callback()

def test(request):

    # check token

    user = User.objects.first()  # Thay thế bằng cách lấy user thực tế
    word = request.GET.get('value')
    language = request.GET.get('lang')
    user_language = request.GET.get('user_lang')

    senses_prefetch = limit_prefetch(
        'senses',
            AISense.objects.select_related('metadata','original').all(),'-created_at',99)

    # if not word:
    #     return Response({'detail': 'Word not found.'}, status=404)
        # return render_all_word_data(user, word, language, user_language)
        # return Response({'detail': 'value is required.'}, status=400)

    queryset = AIWord.objects.filter(value=word, language_code =language).prefetch_related(senses_prefetch).first()

    # Từ không tồn tại trong DB
    if not queryset:
        return render_all_word_data(user, word, language, user_language)
    
    
    senses = queryset.senses.all()
    content_ids = []
    for sense in senses:
        if sense.contents:
            # Nếu đã có contents, cứ thêm vào danh sách tổng
            content_ids.extend(sense.contents)
        else:
            # Nếu không có, lấy từ bản gốc (original)
            if sense.original and sense.original.contents:
                # Lấy list ID từ bản gốc
                original_contents = sense.original.contents 
                
                # Logic: (Original + New) - Old
                # Dùng List Comprehension để lọc (filter) trong Python
                new_ids = getattr(sense, 'new_contents', []) # Tránh lỗi nếu field ko tồn tại
                old_ids = getattr(sense, 'old_contents', [])
                
                # Kết hợp và lọc: lấy item nếu nó nằm trong (Original hoặc New) và KHÔNG nằm trong Old
                combined = list(set(original_contents + new_ids)) # set để tránh trùng
                filtered_contents = [cid for cid in combined if cid not in old_ids]
                
                # Gán lại cho sense để tí nữa Serializer sử dụng
                sense.contents = filtered_contents
                content_ids.extend(filtered_contents)

    contents = AISenseContent.objects.filter(id__in=content_ids, language_code__in=[language, user_language]).all() # .filter(language_code=

    user_language_content = contents.filter(language_code=user_language)

    senses = serialize_senses(senses, contents)
    entries = serialize_entries(senses)
    queryset.processed_entries = entries

    data = AIWordSerializer(queryset).data
    json_bytes = JSONRenderer().render(data)
    json_str = json_bytes.decode("utf-8")

    if(len(user_language_content)==0):
        # Word chưa có nội dung trong ngôn ngữ của người dùng
        return render_translate(user, word, language, user_language, json_str)

    # json_strings = iter([data])
    # json_string = json.dumps(data, default=str,ensure_ascii=False)
    # data = json.loads(json_string)
    return StreamingHttpResponse(
        # data,
        full_data(json_str),
        # content_type="text/event-stream;charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Cho Nginx
            "X-Content-Type-Options": "nosniff", # Ép Cloudflare không buffer
            "Connection": "keep-alive",
        }
    )

def render_all_word_data(user, word, language, user_language):
     # 1. Phân loại ngôn ngữ
    LATIN_LANGS = ['vi', 'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'sv', 'no', 'da', 'fi', 'tr', 'cs', 'hu', 'id']
    SIMPLE_NON_LATIN = ['zh', 'ko', 'ru', 'el', 'ar', 'he', 'hi', 'th']
    
    if language in LATIN_LANGS:
        mode = "latin"
    elif language == 'ja':
        mode = "complex"
    else:
        mode = "nolatin"

    # 2. Lấy Schema và Prompt tương ứng
    current_schema = get_schema(mode)
    current_prompt = get_prompt(mode, word, language, user_language)

    async def generator():
        response = client.models.generate_content_stream(
            # model="gemma-3-1b", 
            model="gemini-2.5-flash-lite",
            contents=current_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=8192, # Tăng từ 2048 lên 8192
                response_mime_type="application/json",
                response_schema=current_schema
            )
        )

        full_response_text = ""
        for chunk in response:
            if chunk.text:
                full_response_text += chunk.text
                yield chunk.text
                await asyncio.sleep(0.001) # Nhả for, giúp event stream gửi dữ liệu ngay lập tức

        
        # Bạn có thể dùng sync_to_async nếu hàm lưu DB là đồng bộ
        # await save_to_database(word, full_response_text) 
        data = json.loads(full_response_text)
        await saveword(user,data, language, user_language)

    return StreamingHttpResponse(
        generator(),
        content_type="text/event-stream;charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Cho Nginx
            "X-Content-Type-Options": "nosniff", # Ép Cloudflare không buffer
            "Connection": "keep-alive",
        }
    )

def render_translate(user, word, language, user_language, contents):
    prompt = f"""# ROLE: Translator
    # INPUT: {contents}
    # INPUT LANGUAGE: {language}
    # TARGET LANGUAGE: {user_language}

    # TASK: Translate the dictionary content.
    # OUTPUT RULE: 
    To save tokens, the output MUST ONLY contain the 'id' and the translated fields.
    id is the id of the original content, NEVER change it. 
    DO NOT repeat the original 'text'.
    """

    schema = {
        "type": "OBJECT",
        "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },
                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                        },
                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "id": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                    },
                                    "required": ["id","translate"]
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "id": { 
                                            "type": "STRING",
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                    },
                                    "required": ["id", "translate"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "id": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    }
                                },

                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples",
                                "translations"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING" }, 
    },
    "required": ["word", "entries"]
    }
    def add_user_language_callback():
        response = client.models.generate_content_stream(
            # model="gemma-3-1b", 
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=8192, # Tăng từ 2048 lên 8192
                response_mime_type="application/json",
                response_schema=schema
            )
        )

    # return current contents.
    return StreamingHttpResponse(
        full_data(contents,add_user_language_callback),
        content_type="text/event-stream;charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Cho Nginx
            "X-Content-Type-Options": "nosniff", # Ép Cloudflare không buffer
            "Connection": "keep-alive",
        }
    )

    # send to ai to get translation
    

    # save translation content to db
    # return translation


    return Response({'detail': 'Word exists but has no content in user language.'}, status=404)

def get_schema(mode):
    if mode == "latin":
        return word_schema
    elif mode == "nonlatin":
        return nonlatin_schema
    elif mode == "complex":
        return complex_schema

def get_prompt(mode, word, language, user_language):
    language_name = language_map.get(language, 'en')
    user_language_name = language_map.get(user_language, 'en')

    # if mode == "simple":
    #     return get_word_prompt(word, language, user_language)
    # elif mode == "latin":
    #     return get_latin_prompt(word, language, user_language)
    # elif mode == "complex":
    return f"""
    # INPUTS:
    - "word": {word}
    - "WORD_LANGUAGE": {language_name}
    - "USER_LANGUAGE": {user_language_name}

    # ROLE: You are an expert multilingual lexicographer. 
    # TASK: Analyze the {language_name} EXACT word '{word}' for a learner's dictionary.

    # LANGUAGE RULES:
    1. "definition.text", "usage.text", "examples.text" MUST be written in {language_name}. 
    2. "definitionTranslated", "usageTranslate", "translate", "translations" fields: Must be in {user_language_name}.
    3. "ipas": 
    - For Latin languages: Use Standard IPA (e.g., US/UK).
    - For non-Latin (Japanese, Chinese, Korean): Provide ROMAN (Romaji/Pinyin) and phonetic script.
    4. "pos":
    - MUST be in English

    # CONTENT QUALITY:
    - All information MUST be correct.
    - NEVER use other word have similar sound, words to show instead.
    - NEVER anser with content that you not sure.
    
    - FOLLOW RESTRICLY LANGUAGE RULES.
    - Sense order by frequency.
    - Accuracy: Do not hallucinate antonyms/synonyms. Use null for "audio" if unknown.
    - Image Prompt: "image_describe" should be a 5-10 word English prompt for Unsplash.
    - should_be_saved: This is a dictionary entry. Therefore, this field is TRUE only for single-meaning phrases, not combinations of different words. These are words or phrases that actually carry meaning, not variations, allusions, or rhetorical devices created by other words. 
    - I am paying for this service, please provide full detail for every field

    # FORMATTING:
    - Strictly adhere to the provided JSON schema. 
    - No markdown formatting in the output, just raw JSON.

    # GROUPING RULE:
    - All senses with the same part of speech MUST be grouped into a single entry.
    - Do NOT create multiple entries with the same POS.

    # OUTPUT:
    Synonyms/antonyms/relateds/tags: each item MUST be unique in the list, no more than 5 items per field.
    """


@sync_to_async
def saveword(user, word_instance, language, user_language, data):
    
    entries = data.get('entries', [])
    
    # Các danh sách để bulk create
    metadata_to_create = []
    content_to_create = [] # Chứa definition, usage, example gốc
    
    # Mapping để xử lý các quan hệ sau khi bulk create
    sense_task_list = []
    valid_sense = 0

    with transaction.atomic():
        try:
            model_fields = [f.name for f in AISenseMetadata._meta.get_fields()]

            for entry in entries:
                pos = entry.get('pos')
                for sense in entry.get('senses', []):
                    metadata = sense.get('metadata', {})
                    if not metadata.get("should_be_saved", False):
                        continue

                    valid_sense += 1

                    # Chuẩn bị Metadata
                    clean_data = {k: v for k, v in metadata.items() if k in model_fields}
                    metadata_to_create.append(AISenseMetadata(
                        **clean_data, pos=pos, created_by=user
                    ))

                    # Lưu cấu trúc sense vào bộ nhớ đệm để xử lý sau khi có IDs
                    sense_task_list.append({
                        'raw_sense': sense,
                        'pos': pos
                    })

            # --- BULK 1: Lưu toàn bộ Metadata ---
            # Lưu ý: Một số DB không trả về IDs khi bulk_create, nhưng Django 3.0+ hỗ trợ ignore_conflicts=False sẽ trả về IDs
            metadatas = AISenseMetadata.objects.bulk_create(metadata_to_create)

            # --- CHUẨN BỊ CONTENT (Giai đoạn 1: Tất cả content không có parent) ---
            contents_buffer = []
            
            for i, task in enumerate(sense_task_list):
                s = task['raw_sense']
                # Định nghĩa một helper để tạo object content nhanh
                def make_c(val_dict, type, lang_obj, l_code):
                    return AISenseContent(
                        value=val_dict.get('text') if isinstance(val_dict, dict) else val_dict,
                        type = type,
                        reading=val_dict.get('reading') if isinstance(val_dict, dict) else None,
                        roman=val_dict.get('roman') if isinstance(val_dict, dict) else None,
                        ruby=val_dict.get('ruby') if isinstance(val_dict, dict) else None,
                        language=lang_obj, language_code=l_code, created_by=user
                    )

                # Add 4 loại content (Def, Usage, Example, Trans)
                # Tạm thời chưa add Example_Trans vì nó cần ID của Example (parent)
                task['c_def'] = make_c(s.get('definition', {}), 'definition', language, language.code)
                task['c_usa'] = make_c(s.get('usage', {}), 'usage', language, language.code)
                task['c_trans'] = make_c(s.get('translations', {}), 'translation', user_language, user_language.code)

                task['exemples_count'] = len(s.get('examples', []))

                for i, ex in enumerate(s.get('examples', [])):
                    # Tạo content cho từng example
                    example_content = make_c(ex, 'example', language, language.code)
                    task['c_exe'+str(i)] = example_content
                    contents_buffer.append(task['c_exe'+str(i)])

                contents_buffer.extend([task['c_def'], task['c_usa'], task['c_trans']])

            # --- BULK 2: Lưu toàn bộ Content lớp 1 ---
            AISenseContent.objects.bulk_create(contents_buffer)

            # --- CHUẨN BỊ CONTENT (Giai đoạn 2: Example Translate - Cần parent id) ---
            ex_trans_buffer = []
            for task in sense_task_list:
                s = task['raw_sense']
                def_trans = AISenseContent(
                    parent=task['c_def'], # Bây giờ đã có ID
                    value=s.get('definition', {}).get('translate'),
                    type='definition_translate',
                    language=user_language, language_code=user_language.code, created_by=user
                )
                usa_trans = AISenseContent(
                    parent=task['c_usa'], # Bây giờ đã có ID
                    value=s.get('usage', {}).get('translate'),
                    type='usage_translate',
                    language=user_language, language_code=user_language.code, created_by=user
                )
                for i, ex in enumerate(s.get('examples', [])):
                    ex_trans = AISenseContent(
                        parent=task['c_exe'+str(i)], # Bây giờ déjà có ID
                        value=ex.get('translate'),
                        type='example_translate',
                        language=user_language, language_code=user_language.code, created_by=user
                    )
                    task['c_exe_t'+str(i)] = ex_trans
                    ex_trans_buffer.append(task['c_exe_t'+str(i)])
                task['c_def_t'] = def_trans
                task['c_usa_t'] = usa_trans
                ex_trans_buffer.extend([def_trans, usa_trans])

            AISenseContent.objects.bulk_create(ex_trans_buffer)

            # --- BULK 3: Lưu AISense (Kết nối tất cả IDs) ---
            senses_to_create = []

            for i, task in enumerate(sense_task_list):
                language_content_id = [str(task['c_def'].id),str(task['c_usa'].id)]
                usser_language_content_id = [str(task['c_def_t'].id),str(task['c_usa_t'].id),str(task['c_trans'].id)]
                for j in range(task['exemples_count']):
                    language_content_id.extend([str(task['c_exe'+str(j)].id)])
                    usser_language_content_id.extend([str(task['c_exe_t'+str(j)].id)])

                contents_id = {language.code: language_content_id, user_language.code: usser_language_content_id}
                senses_to_create.append(AISense(
                    word=word_instance,
                    word_value=word_instance.value,
                    metadata=metadatas[i],
                    contents=contents_id,
                    created_by=user
                ))
            
            AISense.objects.bulk_create(senses_to_create)

            if(valid_sense == 0):
                word_instance.status = 'REJECTED'
            else:
                word_instance.is_active = True
                word_instance.status = 'COMPLETED'
            word_instance.save()

            print(f"Successfully bulk saved word: {word_instance.value}")
        except Exception as e:
            print(f"Error in transaction: {e}")
            # Clear sockets
            raise e

        
word_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },
                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "metadata":{
                                    "type":"OBJECT",
                                    "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "Short neutral visual description for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        }
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                        },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                    },
                                    "required": ["text","translate"]
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                    },
                                    "required": ["text", "translate"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    },
                                    "description": "1 Example only"
                                },

                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples",
                                "translations"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING" }, 
    },
    "required": ["word", "entries"]
}

nonlatin_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },

                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                 "metadata":{
                                    "type":"OBJECT",
                                    "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "is_compound": {"type":"BOOLEAN"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "is_correct_language": {"type":"BOOLEAN","description":"is the word in the correct language"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                    "roman": { "type": "STRING" },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "Short neutral visual description for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text","translate"]
                                },

                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text", "translate"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    },
                                    "description": "1 Example only"
                                },
                                
                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING"}
    },
    "required": ["word", "entries"]
}

complex_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },

                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                 "metadata":{
                                     "type":"OBJECT",
                                     "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "is_compound": {"type":"BOOLEAN"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "is_correct_language": {"type":"BOOLEAN","description":"is the word in the correct language"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                    "roman": { "type": "STRING" },
                                                    "reading": { "type": "STRING", "description": "Kana reading" },
                                                    "ruby": {
                                                        "type": "STRING",
                                                        "description": "combine reading and text. example: 会社（かいしゃ）のために"
                                                    },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "Short neutral visual description for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                        },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                        "roman": { "type": "STRING" },
                                        "reading": { "type": "STRING", "description": "Kana reading" },
                                        "ruby": {
                                                "type": "STRING",
                                                "description": "combine reading and text,. example: 会社（かいしゃ）のために"
                                            }
                                    },
                                    "required": ["text","translate", "roman", "reading", "ruby"],
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                        "roman": { "type": "STRING" },
                                        "reading": { "type": "STRING", "description": "Kana reading" },
                                        "ruby": {
                                                "type": "STRING",
                                                "description": "combine reading and text. example: 会社（かいしゃ）のために"
                                            }
                                    },
                                    "required": ["text", "translate", "roman", "reading", "ruby"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    },
                                    "description": "1 Example only"
                                },

                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING"}
    },

    "required": ["word", "entries"]
}


language_map={
    "en":"English",
    "es":"Spanish",
    "fr":"French",
    "de":"German",
    "ja":"Japanese",
    "zh":"Chinese",
    "ko":"Korean",
    "ru":"Russian",
    "it":"Italian",
    "pt":"Portuguese",
    "ar":"Arabic",
    "nl":"Dutch",
    "pl":"Polish",
    "sv":"Swedish",
    "no":"Norwegian",
    "da":"Danish",
    "fi":"Finnish",
    "el":"Greek",
    "tr":"Turkish",
    "cs":"Czech",
    "hu":"Hungarian",
    "vi":"Vietnamese",
    "th":"Thai",
    "hi":"Hindi",
    "id":"Indonesian",
    "he":"Hebrew",
    }

