from channels.generic.websocket import AsyncWebsocketConsumer
import json

class ChatConsumer(AsyncWebsocketConsumer):
    
    async def disconnect(self, close_code):
        try:
            await self.channel_layer.group_discard(
                self.room_group,
                self.channel_name
            )
        except:
            pass

    async def connect(self):
        # Chỉ chấp nhận kết nối, chưa sub vào đâu cả
        await self.accept()

        # 2. TỰ ĐỘNG JOIN ROOM TEST (Để debug nhanh)
        # Bạn có thể comment dòng này lại sau khi test xong logic Subscribe từ Client
        self.debug_room = "test" 
        await self.channel_layer.group_add(
            self.debug_room,
            self.channel_name
        )
        
        print(f"🛠️ [DEBUG] Socket {self.channel_name} đã tự động join group: {self.debug_room}")
        
        # Gửi một tin nhắn chào mừng để xác nhận đã vào room
        await self.send(text_data=json.dumps({
            "status": "connected",
            "message": f"Welcome! You are auto-joined to {self.debug_room}"
        }))

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get("action")
        word_room = data.get("word_room") # Ví dụ: "en_apple"

        if action == "subscribe":
            # CLIENT GỬI: {"action": "subscribe", "word_room": "en_apple"}
            await self.channel_layer.group_add(word_room, self.channel_name)
            await self.send(text_data=json.dumps({"status": f"Subscribed to {word_room}"}))

        elif action == "unsubscribe":
            # CLIENT GỬI: {"action": "unsubscribe", "word_room": "en_apple"}
            await self.channel_layer.group_discard(word_room, self.channel_name)

        elif action == "chat_message":
            # Broadcast tin nhắn cho một phòng cụ thể
            await self.channel_layer.group_send(
                word_room,
                {"type": "chat_message", "data": data}
            )

    async def chat_message(self, event):
        data = event["data"]
        
        # 1. Gửi dữ liệu cho Client trước để đảm bảo không mất thông tin cuối
        json_data = json.dumps(data, ensure_ascii=False)
        await self.send(text_data=json_data)
        
        # 2. Xử lý "Chủ động rời Group" (Unsubscribe)
        # Nếu data có flag unsubscribe_room, socket sẽ thoát group đó nhưng vẫn GIỮ KẾT NỐI
        target_room = data.get("unsubscribe_room")
        if target_room:
            await self.channel_layer.group_discard(
                target_room,
                self.channel_name
            )
            print(f"Socket {self.channel_name} chủ động rời group: {target_room}")

        # 3. Thực thi lệnh đóng toàn bộ Socket nếu có flag (giữ nguyên logic cũ)
        if data.get("close_now"):
            await self.close()