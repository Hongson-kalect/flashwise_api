from utils.ai.constain import language_map

def render_word_prompt(word, language_code):
    language_name = language_map.get(language_code, 'en')

    extra_rules = ""
    if language_code == 'ja':
        extra_rules = (
            "6. Japanese: level is JLPT (N5-N1). 'roman' is the Hepburn romaji of that exact text. "
            "ipas has one item per reading: label READING, value = the full hiragana reading, roman = its romaji. "
            "Never put furigana or brackets inside any text."
        )

    return f"""
    # ROLE: Expert Lexicographer.
    # TASK: Core analysis of '{word}' ({language_name}) forlearners.

    # RULES:
    1. Language: Content (def, usage, examples) in {language_name}; IPA/POS in English.
    2. Grouping: One entry per POS. Senses ordered by frequency.
    3. Level: Defs/Examples must match the word's level.
    4. Validation: 'should_be_saved' is FALSE for typos or random word strings.
    5. Usage: ONLY a short note on grammar patterns, prepositions, collocations or register (e.g. "Used with 'in'; usually plural"). NEVER example sentences; leave it empty if there is nothing useful to say.
    {extra_rules}

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

