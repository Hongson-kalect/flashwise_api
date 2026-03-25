from utils.ai.constain import language_map

def render_word_prompt(mode, word, language_code, user_language_code):
    language_name = language_map.get(language_code, 'en')
    user_language_name = language_map.get(user_language_code, 'en')

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
    - The definition and examples must use vocabulary at the same level as the word's level
    
    - FOLLOW RESTRICLY LANGUAGE RULES.
    - Sense order by frequency.
    - Accuracy: Do not hallucinate antonyms/synonyms. Use null for "audio" if unknown.
    - Image Prompt: "image_keywords" is list of tags for image prompt.
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
