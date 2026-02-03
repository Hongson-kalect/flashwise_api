# middleware/activity_logger.py
import json
from django.utils.deprecation import MiddlewareMixin
from django.db import transaction

from tracking.models.ModifierLog import ModifierLog
from tracking.models.QueryLog import QueryLog

class ActivityLoggerMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # Tạm lưu thông tin để dùng sau
        request._activity_log_data = {
            "method": request.method,
            "path": request.path,
            "user": request.user if request.user.is_authenticated else None,
            "meta": {
                "query_params": request.GET.dict(),
                "body": self._get_body(request),
            }
        }

    def process_response(self, request, response):
        log_data = getattr(request, "_activity_log_data", None)
        
        if log_data and log_data["user"]:
            try:
                with transaction.atomic():
                    if(log_data["method"] == "GET"):
                        QueryLog.objects.create(
                            user=log_data["user"],
                            path=log_data["path"],  # thêm dòng này
                            target_type=self._infer_target_type(log_data["path"]),
                            target_id=self._infer_target_id(log_data["path"]),
                            meta=log_data["meta"],
                            is_success=response.status_code < 400
                        )
                    else:
                        ModifierLog.objects.create(
                            user=log_data["user"],
                            path=log_data["path"],  # thêm dòng này
                            method = log_data["method"],
                            target_type=self._infer_target_type(log_data["path"]),
                            target_id=self._infer_target_id(log_data["path"]),
                            action=log_data["method"],
                            meta=log_data["meta"],
                            is_success=response.status_code < 400
                        )
            except Exception:
                pass  # Không làm gián đoạn request nếu ghi log lỗi
        return response

    def _get_body(self, request):
        try:
            if request.body:
                return json.loads(request.body.decode("utf-8"))
        except Exception:
            return {}
        return {}

    def _infer_target_type(self, path):
        # Ví dụ: /api/collections/123/ → "collection"
        parts = path.strip("/").split("/")
        return parts[1] if len(parts) > 1 else "unknown"

    def _infer_target_id(self, path):
        parts = path.strip("/").split("/")
        return parts[2] if len(parts) > 2 else None
