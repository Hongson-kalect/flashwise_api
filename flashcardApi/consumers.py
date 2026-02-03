from channels.generic.websocket import AsyncWebsocketConsumer
import json

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group = f"{self.room_id}"

        print(f"Client {self} connected to room: {self.room_id}")

        await self.channel_layer.group_add(
            self.room_group,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group,
            self.channel_name
        )

    async def receive(self, text_data):
        data = json.loads(text_data)

        # broadcast cho cả phòng
        await self.channel_layer.group_send(
            self.room_group,
            {
                "type": "chat_message",
                "data": data # Gửi nguyên dữ liệu nhận được,
            }
        )

    async def chat_message(self, event):
        data = event["data"]
        
        # Gửi dữ liệu cho Client
        json_data = json.dumps(data, ensure_ascii=False)
        
        await self.send(text_data=json_data)
        
        # Thực thi lệnh đóng nếu có flag
        if data.get("close_now"):
            await self.close()
