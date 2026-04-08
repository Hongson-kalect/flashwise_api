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
            'metadata': AISenseMetadataSerializer(sense.metadata).data if sense.metadata else None,
            'definition': sense.processed_definition,
            'usage': sense.processed_usage,
            'examples': sense.processed_examples,
            'translations': sense.processed_translations,
            'collocations': sense.processed_collocations,
            'idioms': sense.processed_idioms,
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