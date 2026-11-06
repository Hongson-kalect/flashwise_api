from utils.ai.constain import language_map

# Bước 1 (Claude Haiku, không thinking): system ngắn cố định, chi tiết theo từ nằm ở prompt user
WORD_SYSTEM_PROMPT = (
    "You are an expert lexicographer writing entries for a learner's dictionary. "
    "Fill the JSON fields in schema order: metadata first, then senses."
)


def render_word_prompt(word, language_code):
    """Prompt bước 1 cho Claude Haiku: quy tắc ngắn, dữ liệu trong thẻ XML. Cấu trúc JSON do schema (structured output) lo."""
    language_name = language_map.get(language_code, 'en')

    extra_rules = ""
    ipa_rule = "\n- ipas: IPA between slashes, e.g. /bæŋk/; for English label it US or UK (give both if they differ)."
    if language_code == 'ja':
        ipa_rule = ""
        extra_rules = """
<japanese>
- level: JLPT N5-N1.
- roman: Hepburn romaji of exactly that text.
- ipas: one item per reading; label READING, value = the full hiragana reading, roman = its romaji.
- Never put furigana or brackets inside any text.
</japanese>"""

    return f"""Write the dictionary entry for the {language_name} word '{word}'.

<rules>
- Validity: set should_be_saved to false and return no senses if '{word}' is a typo, gibberish, not a real {language_name} word or expression, OR an ordinary sentence or question that someone just wrote. Save only a single word, a compound, a phrasal verb or other fixed multi-word expression, an idiom, a proverb or saying, or a common set phrase that dictionaries list.
- Senses: cover the meanings a learner dictionary (Oxford, Cambridge, Longman) lists for the word, most common first, each with its own pos. Include standard everyday and scientific meanings; skip rare, archaic, slang and idiom-only ones. One sense = one meaning: never merge two unrelated meanings, never split one into near-duplicates.
- Language: definition, usage and examples in {language_name}; pos, level and register in English.
- pos: lowercase English term (noun, verb, adjective, adverb, ...). A noun used before another noun (kitchen table) stays a noun: no adjective sense for it.
- definition: one clear sentence, in simpler words than '{word}'; never use '{word}' or its forms in it.
- usage: a short note on grammar patterns, prepositions / particles, collocations or register, e.g. "Usually plural; used with 'in'". About this sense only, never an example sentence. Only write what you are sure of: a wrong note is worse than none, so use "" if there is nothing useful.
- examples: 1-2 natural sentences at the sense's level that show exactly this sense (not another one), each ONE sentence containing '{word}' or an inflected form. With two examples use a different form of '{word}' in each when it sounds natural (e.g. base form in one, -ing or past in the other); never force it. Never reuse an example in another sense.
- level: CEFR A1-C2 (TOPIK1-6 for Korean).
- register: ["neutral"] unless the sense is marked.{ipa_rule}
</rules>{extra_rules}"""


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
- image_subject: the plainest English phrase for what ONE simple illustration of THIS sense shows (lowercase, 1-6 words, no articles). It is a key shared by words of every language, so the same idea must get identical wording: a thing -> only its basic name, no adjectives, material, state or scene ("refrigerator", never "white kitchen refrigerator"; "telescope", never "refracting telescope"; "umbrella", never "open umbrella"); an action -> "person" plus the -ing verb ("person running", "person sleeping"; never child / man / woman, no place); an idea or feeling -> one concrete symbolic scene in the simplest wording ("bird flying out of open cage"). Tell homonyms apart with one extra noun ("bat animal", "baseball bat", "river bank", "money bank").
- image_kind: object, action, scene or abstract; none when nothing can be drawn (grammar words, particles, names, formulas).
At most 5 items per list. Leave a list empty rather than invent or reuse items from another sense."""
