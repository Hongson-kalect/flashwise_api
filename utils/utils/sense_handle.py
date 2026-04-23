from ai.serializers.AISenseMetadata import AISenseMetadataSerializer

# Chuyển hóa sense(contents) -> sense(definition, usage, examples, translations)
def serialize_senses(senses, contents, language_code, user_language_code):
    # 1. Tạo Map để truy xuất Content instance bằng ID cực nhanh (O(1))
    c_map = {str(c.id): c for c in contents}
    new_senses = []

    for sense in senses:
        struct = sense.contents or {}
        
        # Hàm helper để biến Content Instance thành Dict cho Frontend
        def get_content_data(cid):
            c = c_map.get(str(cid))
            if not c: return None
            return {
                'id': c.id,
                'value': c.value,
                'reading': getattr(c, 'reading', None),
                'roman': getattr(c, 'roman', None),
                'ruby': getattr(c, 'ruby', None),
                'audio': getattr(c, 'audio', None), # Tiện thể lấy luôn audio
            }

        # --- PHẦN 1: DEFINITION & USAGE ---
        def process_node(node_name):
            node = struct.get(node_name, {})
            if not node: return None

            if isinstance(node, dict):
                orig_id = node.get(language_code)
                trans_id = node.get(user_language_code)
                
                data = get_content_data(orig_id) or {}
                if trans_id:
                    trans_obj = c_map.get(str(trans_id))
                    if trans_obj:
                        data['translate'] = trans_obj.value
                        data['translate_id'] = trans_obj.id
            else :
                data = get_content_data(node) or None

            return data

        definition = process_node('definition')
        usage = process_node('usage')
        collocations = process_node('collocations')
        idioms = process_node('idioms')

        # --- PHẦN 2: TRANSLATIONS (Bản dịch tổng quát) ---
        # Lưu ý: Nếu translations lưu ID trỏ tới Content chứa list strings
        trans_node = struct.get('translations', {})
        trans_id = trans_node.get(user_language_code)
        translations = []
        if trans_id:
            c_trans = c_map.get(str(trans_id))
            if c_trans:
                # Nếu content.value lưu list JSON
                translations = c_trans.value if isinstance(c_trans.value, list) else [c_trans.value]

        # --- PHẦN 3: EXAMPLES (Ghép cặp cực nhanh không cần parent_id) ---
        examples = {}
        for id, ex_node in struct.get('examples', {}).items():
            orig_id = ex_node.get(language_code)
            trans_id = ex_node.get(user_language_code)
            
            ex_data = get_content_data(orig_id)
            if ex_data:
                examples[orig_id] = ex_data
                
                if trans_id:
                    t_obj = c_map.get(str(trans_id))
                    if t_obj:
                        examples[orig_id]['translate'] = t_obj.value
                        examples[orig_id]['translate_id'] = t_obj.id

                
        # Gán kết quả đã xử lý vào object sense
        sense.processed_definition = definition
        sense.processed_usage = usage
        sense.processed_examples = examples
        sense.processed_translations = translations
        sense.processed_collocations = collocations
        sense.processed_idioms = idioms
        new_senses.append(sense)

    return new_senses

# Chuyển hóa sense[] -> entries(pos,sense[]) theo pos
def serialize_entries(senses):
    entries = []
    pos_map = {}
    for sense in senses:
        
        pos = sense.metadata.pos if sense.metadata else "unknown"

        # Đang chỉnh để thêm sense vào entries với pos + sense

        sense_data = {
            'id': str(sense.id),
            "contents": sense.contents,
            'metadata': AISenseMetadataSerializer(sense.metadata).data if sense.metadata else None,
            "preview": sense.preview,
            # 'definition': sense.processed_definition,
            # 'usage': sense.processed_usage,
            # 'examples': sense.processed_examples,
            # 'translations': sense.processed_translations,
            # 'collocations': sense.processed_collocations,
            # 'idioms': sense.processed_idioms,
        }

        entry = pos_map.get(pos)

        if not entry:
            entry = {
                'pos': pos,
                'senses': [sense_data]
            }
            pos_map[pos] = entry
            entries.append(entry)
        else:
            entry['senses'].append(sense_data)
    return entries

def get_word_lang_content(language_code, contents):
    entries = contents['entries']
    missing_content = []

    # user_lang_entries = []
    for entry in entries:
        senses = entry['senses']
        for sense in senses:
            sense['definition'] = sense['definition'].get(language_code)
            sense['usage'] = sense['usage'].get(language_code)
            sense['examples'] = [example.get(language_code) for example in sense['examples']]

            missing_content.push({
                'id': sense['id'],
                'definition': sense['definition'],
                'usage': sense['usage'],
                'examples': sense['examples'],
                'translations': sense['definition'].get('value'),
            })

    return entries, missing_content

import copy
def get_user_lang_content(language_code, user_language_code, contents):
    copy_contents = copy.deepcopy(contents)
    entries = copy_contents['entries']
    user_entries = []
    missing_content = []
    current_senses =[]

    # user_lang_entries = []
    for entry in entries:
        senses = entry['senses']
        for sense in senses:
            current_senses.append(copy.deepcopy(sense))
            sense['contents'], missing = get_user_lang_sense(language_code, user_language_code, sense['contents'], sense['id'])
            if missing:
                missing_content.append(missing)
    return entries, missing_content, current_senses

def get_user_lang_sense(language_code, user_language_code, sense, sense_id = None):

    if isinstance(sense, list):
            new_list = []
            missing_ex = []
            for index,item in enumerate(sense, 1):
                if isinstance(item, dict):
                    lang_content, is_have_translate = get_user_lang_sense(language_code, user_language_code, item)

                    if lang_content:
                        new_list.append(lang_content)

                        if not is_have_translate:
                            missing_ex.append({"index":index,**lang_content})

            return new_list, missing_ex
    
    lang_content = sense.get(language_code)
    if lang_content:

        res = {language_code: lang_content}
        user_lang_content = sense.get(user_language_code)

        if not user_lang_content: return res, False

        res[user_language_code] = user_lang_content

        return res, True
    
    if not sense_id: return None, False
    
    definition, is_have_def_translate = get_user_lang_sense(language_code, user_language_code, sense['definition'])
    usage, is_have_usage_translate = get_user_lang_sense(language_code, user_language_code, sense['usage'])
    examples, ex_missing_translates = get_user_lang_sense(language_code, user_language_code, sense['examples'])
    translations = sense['translations'].get(user_language_code)

    print(definition, usage, examples)

    if not definition or not usage or not examples: return False


    missing ={}
    content_missing = {}

    if not is_have_def_translate:
        content_missing['definition'] = definition

    if not is_have_usage_translate:
        content_missing['usage'] = usage

    if ex_missing_translates:
        content_missing['examples'] = ex_missing_translates

    if not translations:
        content_missing['translations'] = definition.get(language_code,{}).get('value')

    print(content_missing)

    if content_missing:
        missing={
            'id':sense_id,
            'contents':content_missing
        }

        # Nếu ko gán false thì nó sẽ dịch full base lang
        if not missing['contents'].get('translations'):
            missing['contents']['translations'] = False


    return {
        'definition': definition,
        'usage': usage,
        'examples': examples,
        'translations':translations,

        'collocations':sense['collocations'],
        'idioms':sense['idioms'],
    }, missing
    for key, content in contents.items():
        valid_content = {}

        
    
        lang_content = content.get(language_code)
        user_lang_content = content.get(user_language_code)

        if not lang_content or not user_lang_content: return False

        valid_content[language_code] = lang_content
        valid_content[user_language_code] = user_lang_content

        new_contents[key] = valid_content
    

    return new_contents