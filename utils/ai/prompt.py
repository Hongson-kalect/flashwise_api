from utils.ai.constain import language_map

# Bước 1 (Claude Haiku, không thinking): system ngắn cố định, chi tiết theo từ nằm ở prompt user
WORD_SYSTEM_PROMPT = (
    "You are an expert lexicographer writing entries for a learner's dictionary. "
    "Fill the JSON fields in schema order: metadata first, then senses."
)


REFERENCE_MAX_ENTRIES = 6
REFERENCE_MAX_GLOSSES = 2
REFERENCE_MAX_GLOSS_CHARS = 160


def render_reference(reference):
    """
    Mục từ của từ điển khác (Wiktionary, qua file seed) làm GỢI Ý, chưa kiểm chứng: [{'pos', 'glosses': [...], 'tags': [...]}].
    Cắt gọn để không phình prompt; rỗng / sai dạng thì trả '' (prompt chạy như cũ).
    """
    lines = []
    for entry in (reference or [])[:REFERENCE_MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        glosses = [str(g).strip()[:REFERENCE_MAX_GLOSS_CHARS] for g in (entry.get('glosses') or [])[:REFERENCE_MAX_GLOSSES] if str(g).strip()]
        pos = str(entry.get('pos') or '').strip()[:20]
        if not pos or not glosses:
            continue
        tags = [str(t)[:20] for t in (entry.get('tags') or [])[:4]]
        lines.append(f"- {pos}: " + " | ".join(glosses) + (f" [{', '.join(tags)}]" if tags else ""))
    return "\n".join(lines)


def render_word_prompt(word, language_code, reference=None):
    """Prompt bước 1 cho Claude Haiku: quy tắc ngắn, dữ liệu trong thẻ XML. Cấu trúc JSON do schema (structured output) lo."""
    language_name = language_map.get(language_code, 'en')
    reference_text = render_reference(reference)
    reference_block = f"""

<reference>
{reference_text}
</reference>""" if reference_text else ""
    reference_rule = """
- reference: entries from another dictionary, unverified: unordered, may list archaic, dialect, slang or wrong senses, and its wording is long and clumsy. Use it only to avoid missing a common meaning and to check that a part of speech really exists for this word. Write every definition yourself in simple words; never copy or lightly reword its text. Skip archaic, obsolete, dialect and rare senses even if it lists them. Where it disagrees with your own knowledge, trust yours.""" if reference_text else ""

    extra_rules = ""
    ipa_rule = "\n- ipas: IPA between slashes, e.g. /bæŋk/; for English label it US or UK (give both if they differ)."
    if language_code == 'ja':
        ipa_rule = ""
        extra_rules = """
<japanese>
- level: JLPT N5-N1.
- ipas: one item per reading; label READING, value = the full hiragana reading for that sense.
- Never put romaji, furigana or brackets inside any text (the server adds them).
</japanese>"""
    elif language_code == 'zh':
        ipa_rule = ""
        extra_rules = """
<chinese>
- level: HSK 1-6 (or CEFR A1-C2 if unsure).
- ipas: exactly one item; label READING, value = pinyin with tone marks for THIS sense (syllables separated by spaces, e.g. "yín háng"), not IPA.
- Never put pinyin or brackets inside any text (the server adds them).
</chinese>"""
    elif language_code == 'ko':
        ipa_rule = ""
        extra_rules = """
<korean>
- ipas: exactly one item; label READING, value = the headword in Hangul.
- Never put romanization or brackets inside any text (the server adds them).
</korean>"""

    return f"""Write the dictionary entry for the {language_name} word '{word}'.

<rules>
- Validity: set should_be_saved to false and return no senses if '{word}' is a typo, gibberish, not a real {language_name} word or expression, OR an ordinary sentence or question that someone just wrote. Save only a single word, a compound, a phrasal verb or other fixed multi-word expression, an idiom, a proverb or saying, or a common set phrase that dictionaries list.
- Sensitive: set sensitive to true (and should_be_saved to false, no senses) if '{word}' is a slur or insult aimed at people or a group, hate speech, or a politically or territorially sensitive term (names of leaders used as labels or insults, disputed-territory claims, propaganda or extremist slogans). Ordinary profanity and vulgar words are NOT sensitive: write them normally and mark their senses is_offensive.
- Senses: cover the meanings a learner dictionary (Oxford, Cambridge, Longman) lists for the word, most common first, each with its own pos. Include standard everyday and scientific meanings; skip rare, archaic, slang and idiom-only ones. One sense = one meaning: never merge two unrelated meanings, never split one into near-duplicates.
- Inflected forms: never write a sense whose only meaning is 'plural / past tense / participle / -ing form / comparative of another word': those forms are generated elsewhere. If '{word}' is itself an inflected form of another word, write only the senses it has as a word of its own (for example 'saying' as a noun, 'killed' as an adjective).
- Language: definition, usage and examples in {language_name}; pos, level and register in English.
- pos: lowercase English term (noun, verb, adjective, adverb, ...). A noun used before another noun (kitchen table) stays a noun: no adjective sense for it.
- definition: one clear sentence, in simpler words than '{word}'; never use '{word}' or its forms in it.
- usage: a short note on grammar patterns, prepositions / particles, collocations or register, e.g. "Usually plural; used with 'in'". About this sense only, never an example sentence. Only write what you are sure of: a wrong note is worse than none, so use "" if there is nothing useful.
- examples: 1-2 natural sentences at the sense's level that show exactly this sense (not another one), each ONE sentence containing '{word}' or an inflected form. With two examples use a different form of '{word}' in each when it sounds natural (e.g. base form in one, -ing or past in the other); never force it. Never reuse an example in another sense.
- level: CEFR A1-C2 (TOPIK1-6 for Korean).
- register: ["neutral"] unless the sense is marked.{ipa_rule}{reference_rule}
</rules>{extra_rules}{reference_block}"""


def render_enhanced_prompt(word, language_code, senses):
    """Prompt bước 4 (metadata) cho Gemini flash-lite: ngắn, mỗi danh sách 1 dòng định nghĩa rõ, có khoá nghĩa để model không lẫn."""
    language_name = language_map.get(language_code, 'en')
    sense_lines = "\n".join(f"{key}: ({s['pos']}) {s['definition']}" for key, s in senses.items())

    return f"""Add metadata to each sense of the {language_name} word '{word}'.

Senses (key: (pos) definition):
{sense_lines}

For each sense key, about THAT sense only:
- synonyms / antonyms: {language_name} words with the same / opposite meaning.
- relateds: closely related {language_name} words (same topic or word family).
- collocations: common phrases containing '{word}', e.g. "heavy rain".
- idioms: idioms or fixed expressions with '{word}' in this sense.
- tags: 1-5 short lowercase English topic tags, e.g. "finance", "nature".
- image_keywords: 1-3 English stock-photo search terms that show this sense concretely.
At most 5 items per list. Leave a list empty rather than invent or reuse items from another sense."""
