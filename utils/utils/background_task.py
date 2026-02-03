import asyncio
import threading

def background_task(task):
    coro = task
    thread = threading.Thread(target=run_async_task, args=(coro,))
    thread.daemon = True
    thread.start()

def run_async_task(coro):
    try:
        asyncio.run(coro)
        print("Thread Loop Closed")
    except Exception as e:
        print(f"Thread Loop Error: {e}")
        import traceback
        traceback.print_exc()