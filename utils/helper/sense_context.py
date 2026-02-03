from dataclasses import dataclass, field
from typing import List, Dict, Any

@dataclass
class SenseContext:
    raw: dict
    pos: str
    metadata: object = None
    # Lưu các content đơn lẻ (definition, usage, v.v.)
    obj_map: Dict[str, Any] = field(default_factory=dict)
    # Lưu danh sách các cặp ví dụ (mỗi cặp gồm orig và trans)
    example_objs: List[Dict[str, Any]] = field(default_factory=list)
    
    # Các trường cũ nếu bạn vẫn muốn giữ để tương thích code khác
    contents: Dict[str, object] = field(default_factory=dict)
    examples: List[object] = field(default_factory=list)
    example_trans: List[object] = field(default_factory=list)