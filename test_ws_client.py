# Save as test_client.py
import asyncio
import websockets
import json


async def test_client():
    uri = "ws://192.168.5.223:8190"  # **Update it to the IP ofg Comfy Server and the machine where the mian python relay is running from
    async with websockets.connect(uri) as websocket:
        print("Connected! Waiting for messages...")

        while True:
            try:
                message = await websocket.recv()

                if isinstance(message, bytes):
                    print(f"Received BINARY image: {len(message)} bytes")
                else:
                    # Text message (JSON)
                    data = json.loads(message)
                    print(f"Event: {data.get('type', 'unknown')}")

            except Exception as e:
                print(f"Error: {e}")
                break


asyncio.run(test_client())
