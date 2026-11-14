from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from ai.validators import is_lang
from utils.ai import speech
from utils.utils.permissions import NotBanned, SpeechThrottle

# 8 giây AAC mono 16 kHz chỉ cỡ vài chục KB; 1MB là trần rộng, chặn file lạ / quá dài
MAX_AUDIO_BYTES = 1024 * 1024


class SpeechRecognizeView(APIView):
    """
    Nhận dạng giọng nói cho máy không có bộ nhận dạng sẵn: app ghi âm vài giây rồi gửi lên đây.
    POST speech/recognize/ (multipart): `file` (âm thanh aac / mp3 / wav / ogg / flac, <= 1MB), `lang` (mã ngôn ngữ người học đang nói).
    Trả 200 `{text, empty}`: `text` là chữ nghe được (chép đúng, không sửa phát âm sai), `empty: true` khi không có tiếng nói.
    Âm thanh không được lưu. Lỗi: 400 `file_missing` / `invalid_lang` / `invalid_file`, 413 `file_too_large`, 429 (throttle `speech`), 503 `speech_unavailable`.
    """
    permission_classes = [IsAuthenticated, NotBanned]
    throttle_classes = [UserRateThrottle, SpeechThrottle]

    def post(self, request):
        lang = request.data.get('lang')
        if not is_lang(lang):
            return Response({'detail': 'Invalid lang', 'errorCode': 'invalid_lang'}, status=status.HTTP_400_BAD_REQUEST)
        upload = request.FILES.get('file')
        if upload is None:
            return Response({'detail': 'file is required', 'errorCode': 'file_missing'}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_AUDIO_BYTES:
            return Response({'detail': 'File too large', 'errorCode': 'file_too_large'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if upload.size == 0:
            return Response({'detail': 'Empty file', 'errorCode': 'invalid_file'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            text = speech.transcribe(upload.read(), upload.content_type, lang)
        except ValueError:
            return Response({'detail': 'Unsupported audio type', 'errorCode': 'invalid_file'}, status=status.HTTP_400_BAD_REQUEST)
        except speech.SpeechUnavailable:
            return Response({'detail': 'Speech recognition unavailable', 'errorCode': 'speech_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'text': text, 'empty': not text})
