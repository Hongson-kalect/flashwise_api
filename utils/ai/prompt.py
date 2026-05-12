from utils.ai.constain import language_map

def render_word_prompt(word, language_code):
    language_name = language_map.get(language_code, 'en')

    # add rule for non latin
    # add ruby for japanese 

    return f"""
    # ROLE: Expert Lexicographer.
    # TASK: Core analysis of '{word}' ({language_name}) forlearners.

    # RULES:
    1. Language: Content (def, usage, examples) in {language_name}; IPA/POS in English.
    2. Grouping: One entry per POS. Senses ordered by frequency.
    3. Level: Defs/Examples must match the word's level.
    4. Validation: 'should_be_saved' is FALSE for typos or random word strings.

    # OUTPUT:
    Strictly follow the Core JSON Schema.
    Example must include '{word}'.
    """

    #  - For non-Latin (Japanese, Chinese, Korean): Provide ROMAN (Romaji/Pinyin) and phonetic script.

def render_enhanced_prompt(word, language_code, senses):
    language_name = language_map.get(language_code, 'en')

    # Ví dụ truyền dữ liệu từ Query 1 vào
    context_data = [f"POS: {s_contents['pos']}, Definition: {s_contents['definition']}" for s_id, s_contents in senses.items()]

    return f"""
    # ROLE: Linguistic Consultant & Visual Designer.
    # CONTEXT: 
    Word: '{word}' ({language_name})
    Senses: {context_data}

    # TASK: Enhance the word above with synonyms, idioms, and visual metadata.

    # RULES:
    1. Image Keywords: MUST be in English. Focus on concrete, searchable stock-photo tags.
    2. Relevance: Idioms and synonyms must strictly relate to the specific senses provided.
    3. Limits: Max 5 unique items per list.

    # OUTPUT:
    Strictly follow the Enhanced JSON Schema.
    """

