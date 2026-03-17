import django
from ai.models.AISenseContent import AISenseContent
from utils.utils import uuidv7

def patch_and_clone_contents(struct, update_data, user):
    """
    update_data: [{'id': 'uuid_1', 'value': 'new text'}, ...]
    """

    print('cccccccc',update_data)
    # 1. Chuyển list thành dict để tra cứu O(1)
    # update_map = {str(item['id']): item['value'] for item in update_data}
    
    new_contents_to_create = []

    def recursive_patch(data):
        if isinstance(data, dict):
            new_node = {}
            for lang_key, value in data.items():
                val_str = str(value)
                
                if val_str in update_data:
                    # TẠO CLONE CHO CONTENT CẦN SỬA
                    new_id = uuidv7.generate_uuid7()
                    
                    # Giả định AISenseContent đã được import
                    time = django.utils.timezone.now()
                    new_contents_to_create.append(AISenseContent(
                        id=new_id,
                        value=update_data[val_str],
                        language_code=lang_key, # Lấy luôn lang từ key của JSON (en, vi, ja...)
                        created_by=user,
                        created_at=time,
                        updated_at=time
                    ))
                    new_node[lang_key] = str(new_id)
                else:
                    # GIỮ NGUYÊN HOẶC ĐỆ QUY TIẾP
                    new_node[lang_key] = recursive_patch(value)
            return new_node
            
        elif isinstance(data, list):
            return [recursive_patch(item) for item in data]
        
        return data

    new_struct = recursive_patch(struct)
    return new_struct, new_contents_to_create