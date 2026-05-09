def render_translate_schema(word, word_lang, sense_object: dict, translate_lang:list):
    properties = {}
    required = []

    print('sense_object', sense_object)

    def render_obj(key, type="STRING"):
        # Tạo properties cho từng ngôn ngữ
        lang_props = {
            lang: {
                "type": type
            } if type == "STRING" else {"type": "ARRAY", "items": {"type": 'STRING'}} for lang in translate_lang
        }
        return {
            "type": "OBJECT",
            "properties": lang_props,
            "required": translate_lang,
            # "description": f"Translate {key} to {', '.join(translate_lang)}"
        }
    
    def render_translate_obj(word, definition, type="STRING" ):
        # Tạo properties cho từng ngôn ngữ
        lang_props = {
            lang: {
                "type": type
            } if type == "STRING" else {"type": "ARRAY", "items": {"type": 'STRING'}} for lang in translate_lang
        }
        return {
            "type": "OBJECT",
            "properties": lang_props,
            "required": translate_lang,
            "description": f"Translate {word} from {word_lang} to {', '.join(translate_lang)} by definition: {definition}"
        }


    for sense_id, sense in sense_object.items():
        # required.append(sense_id) # Tùy chọn: có bắt buộc sense_id này phải có trong response không

        # Tạo object chứa các content_id
        content_properties = {}
        content_required = []
        need_translate = True
        
        for key, item in sense.items():

            print('key', key, 'item', item)

            if key =='translations':
                # item ở đây là định nghĩa của sense hiện tại
                # Khi người dùng cung cấp bản dịch cho định nghĩa nhưng không cung cấp bản dịch
                if item:
                    content_properties["translations"] = render_translate_obj(word, item, "ARRAY")
                    content_required.append("translations")
                    need_translate = False

                # Nếu bằng false thì là nó đã có bản dịch
                else:
                    need_translate = False

                continue

            if key == 'examples':
                example_obj = {}
                example_required = []

                for example_id, example in item.items():
                    example_obj[example_id] = render_obj("example")
                    example_required.append(example_id)
                
                content_properties[key] = {
                    "type": "OBJECT",
                    "properties": example_obj,
                    "required": example_required
                }
                content_required.append(key)
                continue

            content_properties[key] = render_obj(key)
            content_required.append(key)

            # c_id = item['id']
            # content_required.append(c_id)
            # content_properties[c_id] = {
            #             "type": "STRING",
            #             "description": f"Translated text for content {c_id}"
            # }

        # Tạo object chúa các translation
        if need_translate:
            content_properties["translations"] = render_obj(word, "ARRAY")
            content_required.append("translations")

        properties[sense_id] = {
            "type": "OBJECT",
            "properties": content_properties,
            "required": content_required
        }
        required.append(sense_id)

    return {
        "type": "OBJECT",
        "properties": properties,
        "required": required
    }

word_schema = {
    "type": "OBJECT",
    "properties": {
        "metadata": {
            "type": "OBJECT",
            "properties": {
                "should_be_saved": { 
                    "type": "BOOLEAN", 
                    "description": "False if the input is gibberish, a typo that doesn't exist, or non-linguistic noise." 
                },
                "is_common": { "type": "BOOLEAN" },
                "language_confidence": { "type": "NUMBER", "description": "0-1 score of how sure AI is about the language" }
            },
            "required": ["should_be_saved"]
        },
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
                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "value": { "type": "STRING" },
                                    },
                                    "required": ["value"]
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "value": { 
                                            "type": "STRING",
                                            "description": "Technical usage: specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },

                                    },
                                    "required": ["value"],
                                },
                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "value": { "type": "STRING" },
                                        },
                                        "required": ["value"]
                                    },
                                    "maxItems": 2,
                                },
                                "is_offensive":{"type":"BOOLEAN"},
                                "pos":{"type":"STRING"},
                                "level": {
                                    "type": "STRING",
                                    "description": "A1–C2, N1–N5, TOPIC1, etc."
                                },
                                "register": {
                                    "type": "ARRAY",
                                    "items": {
                                    "type": "STRING",
                                    "enum": ["neutral", "formal", "informal", "slang", "vulgar", "technical", "literary", "archaic", "dialect", "humorous"]
                                    },
                                    "description": "Danh sách các sắc thái của từ. Nếu là từ phổ thông, chỉ cần trả về ['neutral']."
                                },
                                "ipas": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "value": { "type": "STRING" },
                                            "label": {
                                                "type": "STRING",
                                                "description": "US, UK, ROMAN, etc."
                                            },
                                        },
                                        "required": ["value", "label"]
                                    }
                                },
                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples",
                                "is_offensive",
                                "pos",
                                "level",
                                "register",
                                "ipas"
                            ]
                        }
                    }
                },
                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING" }, 
    },
    "required": ["word", "entries", "metadata"]
}

def render_enhanced_schema(sense_ids):
    """
    sense_ids: List các ID hoặc Index từ Query 1 (ví dụ: ["sense_1", "sense_2"])
    """
    properties = {}
    for s_id in sense_ids:
        properties[s_id] = {
            "type": "OBJECT",
            "properties": {
                "collocations": {"type": "ARRAY", "items": {"type": "STRING"}},
                "idioms": {"type": "ARRAY", "items": {"type": "STRING"}},
                "synonyms": {"type": "ARRAY", "items": {"type": "STRING"}},
                "antonyms": {"type": "ARRAY", "items": {"type": "STRING"}},
                "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                "image_keywords": {"type": "ARRAY", "items": {"type": "STRING"}}
            },
            "required": ["tags", "image_keywords", "synonyms"] # Ép AI không được bỏ sót
        }
    
    return {
        "type": "OBJECT",
        "properties": properties,
        "required": sense_ids # Đảm bảo AI phải trả đủ data cho mọi sense
    }

nonlatin_schema = {
    "type": "OBJECT",
    "properties": {
        "metadata": {
            "type": "OBJECT",
            "properties": {
                "should_be_saved": { 
                    "type": "BOOLEAN", 
                    "description": "False if the input is gibberish, a typo that doesn't exist, or non-linguistic noise." 
                },
                "is_common": { "type": "BOOLEAN" },
                "language_confidence": { "type": "NUMBER", "description": "0-1 score of how sure AI is about the language" }
            },
            "required": ["should_be_saved"]
        },
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
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "pos":{"type":"STRING"},
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
                                        "image_keywords": {
                                            "type": "STRING",
                                            "description": "1-5 visual-heavy keywords for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_keywords", "level", "pos"]
                                },
                                "collocations": {
                                    "type": "ARRAY",
                                    "items": { "type": "STRING" },
                                    "description": "Common word combinations. e.g. ['heavy rain', 'pour with rain']"
                                },
                                "idioms": {
                                    "type": "ARRAY",
                                    "items": { "type": "STRING" },
                                    "description": "Idiomatic expressions related to this sense."
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
                                        "pos":{"type":"STRING"},
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
                                        "image_keywords": {
                                            "type": "STRING",
                                            "description": "1-5 keywords for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_keywords", "level", "pos"]
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
                                    "required": ["text","translate", "roman"],
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
                                    "required": ["text", "translate", "roman"],
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
                                            "roman": { "type": "STRING" },
                                        },
                                        "required": ["text", "translate", "roman"],
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

