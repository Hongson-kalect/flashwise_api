"""
Nhận dạng giọng nói bằng Gemini cho máy không có bộ nhận dạng sẵn (vd. iQOO / vivo bản Trung Quốc, Android chặn dịch vụ của Google với app thứ ba).
App ghi âm vài giây, gửi lên `POST speech/recognize/`; hàm này hỏi Gemini (nhận file âm thanh) và trả đúng chữ nghe được.

Gemini là mô hình ngôn ngữ nên có xu hướng sửa phát âm sai thành từ đúng. Vì vậy:
- KHÔNG gửi từ đáp án cho Gemini (gửi thì nó thiên về trả đúng từ đó);
- prompt yêu cầu chép đúng điều nghe được, không sửa;
- không có tiếng nói thì trả '' (prompt quy ước `<empty>`), app tính là "không nghe thấy gì". Đo thật với im lặng / tiếng ồn: prompt đơn giản bịa ra "hello" ở cả 2.5 và 3.1 flash-lite, prompt yêu cầu quyết định có giọng người nói rõ hay không trước thì trả rỗng; app còn tự lọc phía client (không gửi khi không nghe thấy tiếng nói).
Âm thanh không được lưu lại ở server.
"""
import logging

from django.conf import settings
from google import genai
from google.genai import types

from utils.ai import usage

logger = logging.getLogger(__name__)

# Tên ngôn ngữ cho prompt (mã ngôn ngữ của app -> tên tiếng Anh). Mã lạ thì dùng nguyên mã.
LANGUAGE_NAMES = {
    'en': 'English', 'vi': 'Vietnamese', 'ja': 'Japanese', 'zh': 'Mandarin Chinese',
    'ko': 'Korean', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
}
EMPTY_MARK = '<empty>'
MAX_OUTPUT_TOKENS = 64

# Định dạng âm thanh Gemini nhận (https://ai.google.dev/gemini-api/docs/audio); app gửi AAC (ADTS) từ expo-av
ALLOWED_MIME = {
    'audio/aac': 'audio/aac', 'audio/x-aac': 'audio/aac', 'audio/mpeg': 'audio/mp3', 'audio/mp3': 'audio/mp3',
    'audio/wav': 'audio/wav', 'audio/x-wav': 'audio/wav', 'audio/ogg': 'audio/ogg', 'audio/flac': 'audio/flac',
    'audio/mp4': 'audio/aac', 'audio/m4a': 'audio/aac',
}


class SpeechUnavailable(Exception):
    """Gemini không trả lời được (lỗi / quá hạn / hết model dự phòng)."""


def _prompt(lang):
    name = LANGUAGE_NAMES.get(lang, lang)
    # Đã đo: câu đầu nhắc "người học nói một từ" hoặc liệt kê "silence, noise..." khiến 2.5 flash-lite bịa "hello" khi im lặng; bản này trả rỗng
    return (
        "This audio comes from a phone microphone. Decide first whether a HUMAN is clearly speaking a word. "
        f"If you cannot clearly hear a human voice saying a real word, answer exactly {EMPTY_MARK}. Never invent, guess or complete a word. "
        f"If speech is present, transcribe exactly what was said in {name}, no correction of pronunciation or grammar (write the closest word actually heard), no punctuation, one line."
    )


def _config(model):
    """Tắt suy nghĩ (nhận dạng không cần): 2.5 dùng thinking_budget=0; 3.x dùng thinkingLevel=minimal qua extra_body. Kèm trần thời gian gọi."""
    timeout_ms = int(settings.AI_SPEECH_TIMEOUT * 1000)
    kwargs = dict(max_output_tokens=MAX_OUTPUT_TOKENS, temperature=0)
    if str(model).startswith('gemini-2.5'):
        return types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=types.HttpOptions(timeout=timeout_ms), **kwargs,
        )
    return types.GenerateContentConfig(
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            extra_body={'generationConfig': {'thinkingConfig': {'thinkingLevel': 'minimal'}}},
        ), **kwargs,
    )


def clean_transcript(text):
    """Làm sạch kết quả của model: bỏ khoảng trắng / dấu nháy thừa, quy ước <empty> thành ''."""
    text = (text or '').strip().strip('"“”\'`').strip()
    if not text or EMPTY_MARK in text.lower():
        return ''
    return ' '.join(text.split())[:200]


def transcribe(audio, mime_type, lang):
    """Trả chữ nghe được ('' nếu không có tiếng nói). Thử lần lượt các model trong GEMINI_SPEECH_MODEL; hết model thì ném SpeechUnavailable."""
    mime = ALLOWED_MIME.get((mime_type or '').lower().split(';')[0].strip())
    if mime is None:
        raise ValueError('unsupported_mime')
    models = settings.GEMINI_SPEECH_MODEL
    last = None
    for model in models:
        try:
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            response = client.models.generate_content(
                model=model,
                contents=[_prompt(lang), types.Part.from_bytes(data=audio, mime_type=mime)],
                config=_config(model),
            )
            usage.record_gemini('speech', response)
            return clean_transcript(response.text)
        except Exception as e:  # lỗi mạng / quá tải / quá hạn: thử model kế
            last = e
            logger.warning('speech transcribe failed on %s: %s', model, e)
    raise SpeechUnavailable(str(last))
