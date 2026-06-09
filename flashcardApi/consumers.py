from channels.generic.websocket import AsyncWebsocketConsumer
import json

class ChatConsumer(AsyncWebsocketConsumer):
    
    async def connect(self):
        # Lưu trữ danh sách các phòng mà kết nối này đã tham gia (Pub/Sub đa phòng)
        self.subscribed_rooms = set()
        
        # Chỉ chấp nhận kết nối, giữ một đường ống duy nhất
        await self.accept()
        print(f"📡 [CONNECTED] Client mới kết nối: {self.channel_name}")

        # TỰ ĐỘNG JOIN ROOM TEST (Nếu muốn giữ để debug)
        self.debug_room = "test_room" 
        await self.channel_layer.group_add(self.debug_room, self.channel_name)
        self.subscribed_rooms.add(self.debug_room) # Lưu lại để dọn dẹp sau này
        
        await self.send(text_data=json.dumps({
            "status": "connected",
            "word_room": self.debug_room, # Trả về word_room để Client map được callback nếu cần
            "message": f"Welcome! Auto-joined to {self.debug_room}"
        }))

    async def disconnect(self, close_code):
        print(f"🔌 [DISCONNECTED] Client ngắt kết nối: {self.channel_name}. Tiến hành dọn dẹp...")
        
        # Bắt buộc: Thoát khỏi TẤT CẢ các phòng đã subscribe để tránh memory leak trên Redis
        for room_id in list(self.subscribed_rooms):
            try:
                await self.channel_layer.group_discard(room_id, self.channel_name)
                print(f"🧹 Đã xóa {self.channel_name} khỏi group: {room_id}")
            except Exception as e:
                print(f"❌ Lỗi khi dọn dẹp room {room_id}: {e}")
                
        self.subscribed_rooms.clear()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            print("❌ Nhận dữ liệu không phải format JSON hợp lệ")
            return

        action = data.get("action")
        word_room = data.get("word_room") # Đây chính là chuỗi MD5_lang gửi từ Expo

        if not word_room:
            print("⚠️ Cảnh báo: Nhận request nhưng thiếu tham số 'word_room'")
            return

        # ---- XỬ LÝ LỆNH SUBSCRIBE ----
        if action == "subscribe":
            print(f"➕ Socket {self.channel_name} VẴN VÀO phòng: {word_room}")
            await self.channel_layer.group_add(word_room, self.channel_name)
            self.subscribed_rooms.add(word_room) # Lưu lại quản lý
            print(f"➕ Socket {self.channel_name} ĐÃ VÀO phòng: {word_room}")
            
            # Phải trả về đúng trường 'word_room' để client nhận diện và kích hoạt callback
            await self.send(text_data=json.dumps({
                "status": "subscribed_success",
                "word_room": word_room,
                "message": f"Hệ thống đã kết nối bạn vào phòng {word_room}"
            }))

        # ---- XỬ LÝ LỆNH UNSUBSCRIBE ----
        elif action == "unsubscribe":
            await self.channel_layer.group_discard(word_room, self.channel_name)
            if word_room in self.subscribed_rooms:
                self.subscribed_rooms.remove(word_room)
            print(f"➖ Socket {self.channel_name} ĐÃ RỜI phòng: {word_room}")

        # ---- XỬ LÝ BROADCAST/CHAT (Dùng để test đẩy dữ liệu chéo giữa các client) ----
        elif action == "chat_message":
            await self.channel_layer.group_send(
                word_room,
                {
                    "type": "chat_handler", # Trùng tên với hàm xử lý bên dưới (thay dấu _ bằng .)
                    "data": data
                }
            )

    # Hàm xử lý khi có tin nhắn từ Group bắn vào (ví dụ bắn từ Django Shell hoặc Client khác)
    async def chat_handler(self, event):
        data = event["data"]

        
        # 1. Forward nguyên gói tin xuống cho Client Expo nhận
        await self.send(text_data=json.dumps(data, ensure_ascii=False))
        
        # 2. Xử lý "Chủ động kick Client khỏi Group từ phía Server" (Nếu data từ server có lệnh)
        target_room = data.get("word_room")
        is_unsubscribe = data.get("unsubscribe")
        if is_unsubscribe  and target_room and target_room in self.subscribed_rooms:
            await self.channel_layer.group_discard(target_room, self.channel_name)
            self.subscribed_rooms.remove(target_room)
            print(f"⚡ Server chủ động mời {self.channel_name} rời khỏi: {target_room}")

        # 3. Thực thi lệnh đóng socket khẩn cấp từ Server
        if data.get("close_now"):
            await self.close()