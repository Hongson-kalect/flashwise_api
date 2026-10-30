"""
Gọi Claude cho bước draft / refine.
Dùng structured outputs (output_config.format) nên JSON trả về luôn đúng schema, kể cả khi stream.
"""
from anthropic import AsyncAnthropic
from django.conf import settings

# Ràng buộc JSON Schema mà structured outputs của Claude không nhận (gửi lên sẽ 400): bỏ, phần cần thiết nói trong prompt / cắt ở code
_UNSUPPORTED = ('maxItems', 'minItems', 'minimum', 'maximum', 'minLength', 'maxLength')


def to_json_schema(schema):
    """Schema kiểu Gemini (type viết hoa) -> JSON Schema cho Claude: type viết thường, mọi object có additionalProperties=false."""
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED:
            continue
        if key == 'type' and isinstance(value, str):
            out[key] = value.lower()
        elif key == 'properties':
            out[key] = {name: to_json_schema(prop) for name, prop in value.items()}
        else:
            out[key] = to_json_schema(value)
    if out.get('type') == 'object':
        out['additionalProperties'] = False
    return out


def output_config(schema, effort=None):
    config = {'format': {'type': 'json_schema', 'schema': to_json_schema(schema)}}
    if effort:
        config['effort'] = effort
    return config


def client():
    # SDK tự thử lại 429 / 5xx; để 1 lần vì ngoài kia còn vòng thử model kế tiếp
    return AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=1)
