"""
Concept tầng 1: nối các nghĩa GIỐNG HỆT nhau giữa các ngôn ngữ (big = lớn = grande) để dùng chung ảnh và làm nền cho tầng 2.
Quy trình mỗi nghĩa: embedding("gloss: định nghĩa en") -> ứng viên (vector + headword trùng gloss) -> Gemini xác nhận N lần (đảo thứ tự)
-> chỉ nối khi mọi lần cùng chọn một concept, nếu không thì tạo concept mới (thiên về tách). Mọi quyết định ghi ConceptDecision.
"""
