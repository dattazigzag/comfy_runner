# File: main.py
import asyncio
import json
import toml
import struct
import websockets
import requests
import os
import sys
import time
from pathlib import Path
import signal
import aiohttp
from aiohttp import web
import colorama
from colorama import Fore, Style

# Initialize colorama for cross-platform colored terminal output
colorama.init()


# Load configuration
def load_config(config_path="config.toml"):
    """Load configuration from TOML file"""
    try:
        config_file = Path(config_path)
        if config_file.exists():
            print(
                f"\n{Fore.BLUE}Loading configuration from:{Style.RESET_ALL} {config_file}"
            )
            return toml.load(config_file)
        else:
            print(f"{Fore.LIGHTRED_EX}Config: {config_file} not found{Style.RESET_ALL}")
            return None
    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error loading config:{Style.RESET_ALL} {e}")
        return None


# Load configuration
CONFIG = load_config()
if not CONFIG:
    print(f"{Fore.LIGHTRED_EX}Exiting...{Style.RESET_ALL}")
    sys.exit(1)

# Extract configuration values with defaults
COMFY_SERVER = CONFIG.get("comfy", {}).get("host", "127.0.0.1")
COMFY_PORT = CONFIG.get("comfy", {}).get("port", 8188)
CURR_WORKFLOW = CONFIG.get("comfy", {}).get("workflow", "workflows/workflow_api.json")
MIDDLEWARE_HTTP_PORT = CONFIG.get("http-server", {}).get("port", 8189)
RELAY_WS_PORT = CONFIG.get("server", {}).get("ws_port", 8190)

# Node mappings from config
NODE_MAPPINGS = CONFIG.get("node_mappings", {})
SAVE_IMAGE_NODE_ID = NODE_MAPPINGS.get("save_image_node", "9")

# Text input field keys that accept string values
TEXT_INPUT_KEYS = [
    "text",
    "value",
    "text_positive",
    "text_negative",
    "prompt",
    "system",
    "style",
    "style_name",
    "key",
    "url",
    "model",
]

# Global state variables
current_prompt_id = None
ws_connection = None
session_id = None
workflow_json = None
execution_status = "idle"
connected_clients = set()

# Display configuration
print(f"\n{Fore.LIGHTBLACK_EX}COMFY_SERVER_HOST:{Style.RESET_ALL} {COMFY_SERVER}")
print(f"{Fore.LIGHTBLACK_EX}COMFY_SERVER_PORT:{Style.RESET_ALL} {COMFY_PORT}")
print(f"{Fore.LIGHTBLACK_EX}CURR_WORKFLOW:{Style.RESET_ALL} {CURR_WORKFLOW}")
print(
    f"{Fore.LIGHTBLACK_EX}MIDDLEWARE_HTTP_PORT:{Style.RESET_ALL} {MIDDLEWARE_HTTP_PORT}"
)
print(f"{Fore.LIGHTBLACK_EX}MIDDLEWARE_WS_PORT:{Style.RESET_ALL} {RELAY_WS_PORT}")
print(f"{Fore.LIGHTBLACK_EX}SAVE_IMAGE_NODE_ID:{Style.RESET_ALL} {SAVE_IMAGE_NODE_ID}")

time.sleep(2)


def signal_handler(sig, frame):
    """Handle Ctrl+C and other termination signals"""
    print(f"{Fore.LIGHTYELLOW_EX}Received termination signal.{Style.RESET_ALL}")
    print(f"{Fore.LIGHTBLACK_EX}Cancelling workflow and exiting...{Style.RESET_ALL}")

    if current_prompt_id:
        cancel_workflow(current_prompt_id)

    print(f"{Fore.LIGHTBLUE_EX}Exiting gracefully.{Style.RESET_ALL}")
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# Utility Functions
def create_json_response(data, status=200):
    """Helper function to create consistent JSON responses"""
    return web.Response(
        text=json.dumps(data), status=status, content_type="application/json"
    )


async def validate_json_request(request, required_fields):
    """Validate JSON request and required fields"""
    try:
        data = await request.json()
        missing_fields = [
            field for field in required_fields if field not in data or not data[field]
        ]
        if missing_fields:
            return None, f"Missing required fields: {', '.join(missing_fields)}"
        return data, None
    except json.JSONDecodeError:
        return None, "Invalid JSON in request body"
    except Exception as e:
        return None, f"Error parsing request: {str(e)}"


def validate_node_id(node_id_str):
    """Validate and convert node_id to integer"""
    try:
        return int(node_id_str), None
    except (ValueError, TypeError):
        return None, "Invalid node_id - must be a number"


# WebSocket Functions
async def handle_websocket_client(websocket):
    """Handle new WebSocket client connections (e.g.: Node-RED)"""
    print(
        f"{Fore.LIGHTGREEN_EX}New WebSocket client connected from {websocket.remote_address}{Style.RESET_ALL}"
    )

    connected_clients.add(websocket)

    try:
        async for message in websocket:
            print(f"Received message from client: {message}")
    except websockets.exceptions.ConnectionClosed:
        print(f"{Fore.LIGHTYELLOW_EX}WebSocket client disconnected{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}WebSocket client error: {e}{Style.RESET_ALL}")
    finally:
        connected_clients.discard(websocket)
        print(
            f"{Fore.LIGHTBLACK_EX}Client removed.{Style.RESET_ALL} Active clients: {len(connected_clients)}"
        )


async def start_websocket_server():
    """Start WebSocket server for Node-RED clients"""
    print(
        f"{Fore.LIGHTYELLOW_EX}Starting WebSocket server on:{Fore.LIGHTBLACK_EX} ws://{COMFY_SERVER}:{RELAY_WS_PORT}{Style.RESET_ALL}"
    )

    server = await websockets.serve(
        handle_websocket_client,
        COMFY_SERVER,
        RELAY_WS_PORT,
        ping_timeout=60,
        ping_interval=30,
    )

    print(
        f"{Fore.LIGHTGREEN_EX}WebSocket server started:{Fore.LIGHTBLACK_EX} ws://{COMFY_SERVER}:{RELAY_WS_PORT}{Style.RESET_ALL}"
    )
    return server


async def connect_comfy_websocket(server, port):
    """Connect to the ComfyUI WebSocket endpoint"""
    global ws_connection, session_id

    if ws_connection:
        try:
            await ws_connection.close()
        except Exception:
            pass

    ws_url = f"ws://{server}:{port}/ws"
    print(
        f"{Fore.LIGHTYELLOW_EX}Connecting to WebSocket at:{Fore.LIGHTBLACK_EX} {ws_url}...{Style.RESET_ALL}"
    )

    try:
        ws_connection = await websockets.connect(
            ws_url, ping_timeout=60, ping_interval=30
        )

        # Receive initial status message to get session ID
        initial_msg = await ws_connection.recv()
        initial_data = json.loads(initial_msg)
        session_id = initial_data.get("data", {}).get("sid")

        if not session_id:
            print(
                f"{Fore.LIGHTRED_EX}Failed to get session ID,{Style.RESET_ALL} using 'default_client' instead"
            )
            session_id = "default_client"
        else:
            print(
                f"{Fore.LIGHTGREEN_EX}Got session ID:{Fore.LIGHTBLACK_EX} {session_id} {Style.RESET_ALL}"
            )

        return ws_connection
    except Exception as e:
        print(
            f"{Fore.LIGHTRED_EX}Failed to connect to WebSocket:{Fore.LIGHTBLACK_EX} \n{e} {Style.RESET_ALL}"
        )
        return None


async def broadcast_to_clients(message, is_binary=False):
    """Broadcast ws message to all connected clients (e.g.: Node-RED)"""
    if not connected_clients:
        return

    clients_copy = connected_clients.copy()
    successful_sends = 0

    for client in clients_copy:
        try:
            await client.send(message)
            successful_sends += 1
        except websockets.exceptions.ConnectionClosed:
            connected_clients.discard(client)
            print(f"{Fore.LIGHTYELLOW_EX}Removed disconnected client{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.LIGHTRED_EX}Error sending to client: {e}{Style.RESET_ALL}")
            connected_clients.discard(client)

    if successful_sends > 0:
        if is_binary:
            print(
                f"{Fore.LIGHTBLACK_EX}Sent binary data ({len(message)} bytes) to {successful_sends} client(s){Style.RESET_ALL}"
            )
        else:
            try:
                msg_data = json.loads(message)
                event_type = msg_data.get("type", "unknown")
                print(
                    f"{Fore.LIGHTBLUE_EX}Broadcast '{event_type}' event to {successful_sends} clients{Style.RESET_ALL}"
                )
            except:
                print(
                    f"{Fore.LIGHTBLUE_EX}Broadcast text data to {successful_sends} clients{Style.RESET_ALL}"
                )


# ComfyUI Helper Functions
def test_comfyui_connection(server_addr, port_num):
    """Test connectivity to ComfyUI server"""
    try:
        url = f"http://{server_addr}:{port_num}/system_stats"
        print(
            f"{Fore.LIGHTYELLOW_EX}Testing connectivity to ComfyUI at{Fore.LIGHTBLACK_EX} {url} {Style.RESET_ALL}"
        )

        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            print(f"{Fore.LIGHTGREEN_EX}ComfyUI connection successful{Style.RESET_ALL}")
            return True
        else:
            print(
                f"{Fore.LIGHTRED_EX}ComfyUI connection failed: {Fore.LIGHTBLACK_EX}{response.status_code}{Style.RESET_ALL}"
            )
            return False
    except Exception as e:
        print(
            f"{Fore.LIGHTRED_EX}Error connecting to ComfyUI: \n{Fore.LIGHTBLACK_EX}{e}{Style.RESET_ALL}"
        )
        return False


def load_workflow_from_file(workflow_file):
    """Load workflow JSON from file without executing it"""
    if not os.path.exists(workflow_file):
        print(
            f"{Fore.LIGHTRED_EX}Error: Workflow file '{workflow_file}' not found.{Style.RESET_ALL}"
        )
        return None

    try:
        with open(workflow_file, "r") as f:
            workflow_data = json.load(f)

        print(
            f"{Fore.LIGHTGREEN_EX}Workflow loaded: {Fore.BLACK}{workflow_file}{Style.RESET_ALL}"
        )
        return workflow_data
    except json.JSONDecodeError:
        print(
            f"{Fore.LIGHTRED_EX}Error: The file {workflow_file} contains invalid JSON.{Style.RESET_ALL}"
        )
        return None
    except Exception as e:
        print(
            f"{Fore.LIGHTRED_EX}Error loading workflow file: \n{Fore.LIGHTBLACK_EX}{e}{Style.RESET_ALL}"
        )
        return None


def update_text_node_with_text(workflow, node_id, text_value):
    """Update any node that accepts text input by node_id"""
    node_id_str = str(node_id)

    if node_id_str not in workflow:
        print(
            f"{Fore.LIGHTRED_EX}Error: Node ID {node_id} not found in workflow.{Style.RESET_ALL}"
        )
        return False

    node = workflow[node_id_str]

    if not isinstance(node, dict) or "inputs" not in node:
        print(
            f"{Fore.LIGHTRED_EX}Error: Node ID {node_id} doesn't have 'inputs' section.{Style.RESET_ALL}"
        )
        return False

    # Try to update with any recognized text input key
    for key in TEXT_INPUT_KEYS:
        if key in node["inputs"]:
            node["inputs"][key] = text_value
            print(
                f"{Fore.LIGHTGREEN_EX}Updated node (ID: {node_id}) '{key}' with:{Style.RESET_ALL} {text_value}"
            )
            return True

    print(
        f"{Fore.LIGHTRED_EX}Error: Node ID {node_id} doesn't have any recognized text input field.{Style.RESET_ALL}"
    )
    return False


def update_image_node_with_image(workflow, node_id, image_name):
    """Find LoadImage node according to node_id in workflow and update it with the new image"""
    node_id_str = str(node_id)

    if node_id_str not in workflow:
        print(
            f"{Fore.LIGHTRED_EX}Error: Node ID {node_id} not found in workflow.{Style.RESET_ALL}"
        )
        return False

    node = workflow[node_id_str]

    if not isinstance(node, dict) or node.get("class_type") != "LoadImage":
        print(
            f"{Fore.LIGHTRED_EX}Error: Node ID {node_id} is not a LoadImage node. Found class_type: {node.get('class_type', 'Unknown')}{Style.RESET_ALL}"
        )
        return False

    node["inputs"]["image"] = image_name
    print(
        f"{Fore.LIGHTGREEN_EX}Updated LoadImage node (ID: {node_id}) with image: {image_name}{Style.RESET_ALL}"
    )
    return True


def cancel_workflow(prompt_id):
    """Cancel workflows using the global interrupt endpoint"""
    try:
        url = f"http://{COMFY_SERVER}:{COMFY_PORT}/interrupt"
        print(
            f"{Fore.LIGHTBLACK_EX}Interrupting all workflows{Style.RESET_ALL} (including prompt ID: {prompt_id})"
        )
        response = requests.post(url, timeout=10)

        if response.status_code == 200:
            print(
                f"{Fore.LIGHTGREEN_EX}Interrupt request sent successfully.{Style.RESET_ALL}"
            )
            return True
        else:
            print(
                f"{Fore.LIGHTRED_EX}Failed to interrupt workflow: {response.status_code}{Style.RESET_ALL}"
            )
            return False
    except Exception as e:
        print(
            f"{Fore.LIGHTRED_EX}Error sending interrupt request:{Style.RESET_ALL} {e}"
        )
        return False


def get_generated_image(prompt_id):
    """Get generated image with robust retry and file verification"""
    max_attempts = 12

    for attempt in range(max_attempts):
        if attempt > 0:
            wait_time = 1 + (attempt * 0.5)
            print(
                f"{Fore.LIGHTYELLOW_EX}Attempt {attempt + 1}/{max_attempts} - waiting {wait_time}s{Style.RESET_ALL}"
            )
            time.sleep(wait_time)

        try:
            url = f"http://{COMFY_SERVER}:{COMFY_PORT}/history/{prompt_id}"
            response = requests.get(url, timeout=10)

            if response.status_code != 200:
                print(
                    f"{Fore.LIGHTYELLOW_EX}History API returned {response.status_code}, retrying...{Style.RESET_ALL}"
                )
                continue

            history_data = response.json()

            if prompt_id not in history_data:
                print(
                    f"{Fore.LIGHTYELLOW_EX}Prompt ID {prompt_id} not in history yet, retrying...{Style.RESET_ALL}"
                )
                continue

            outputs = history_data[prompt_id].get("outputs", {})
            if not outputs:
                print(
                    f"{Fore.LIGHTYELLOW_EX}No outputs in history yet, retrying...{Style.RESET_ALL}"
                )
                continue

            # Extract image using existing logic
            filename, view_url = _extract_image_from_outputs(outputs)

            if filename and view_url:
                # Verify file is actually accessible
                if _verify_image_accessible(view_url):
                    print(
                        f"{Fore.LIGHTGREEN_EX}Image verified: {filename}{Style.RESET_ALL}"
                    )
                    return filename, view_url
                else:
                    print(
                        f"{Fore.LIGHTYELLOW_EX}Image metadata found but file not accessible yet...{Style.RESET_ALL}"
                    )
            else:
                print(
                    f"{Fore.LIGHTYELLOW_EX}No output images found in attempt {attempt + 1}, retrying...{Style.RESET_ALL}"
                )

        except Exception as e:
            print(
                f"{Fore.LIGHTRED_EX}Error in attempt {attempt + 1}: {e}{Style.RESET_ALL}"
            )

    print(
        f"{Fore.LIGHTRED_EX}Failed to get accessible image after {max_attempts} attempts{Style.RESET_ALL}"
    )
    return None, None


def _extract_image_from_outputs(outputs):
    """Extract image info from outputs - extracted from original logic"""
    # First, try the configured save image node
    if SAVE_IMAGE_NODE_ID in outputs:
        node_output = outputs[SAVE_IMAGE_NODE_ID]
        if "images" in node_output:
            for image in node_output["images"]:
                if image["type"] == "output":
                    filename = image["filename"]
                    subfolder = image.get("subfolder", "")
                    image_type = image["type"]

                    # Construct the viewable URL
                    view_url = (
                        f"http://{COMFY_SERVER}:{COMFY_PORT}/view?filename={filename}"
                    )
                    if subfolder:
                        view_url += f"&subfolder={subfolder}"
                    view_url += f"&type={image_type}"

                    return filename, view_url

    # Fallback: search all nodes for output images
    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for image in node_output["images"]:
                if image["type"] == "output":
                    filename = image["filename"]
                    subfolder = image.get("subfolder", "")
                    image_type = image["type"]

                    view_url = (
                        f"http://{COMFY_SERVER}:{COMFY_PORT}/view?filename={filename}"
                    )
                    if subfolder:
                        view_url += f"&subfolder={subfolder}"
                    view_url += f"&type={image_type}"

                    return filename, view_url

    return None, None


def _verify_image_accessible(view_url):
    """Verify image is accessible via HEAD request"""
    try:
        response = requests.head(view_url, timeout=5)
        return response.status_code == 200
    except Exception:
        return False


# Workflow Execution Functions
async def handle_preview_image(binary_data):
    """Handle binary preview image data from ComfyUI WebSocket"""
    try:
        if len(binary_data) < 8:
            print(
                f"{Fore.LIGHTYELLOW_EX}Received short binary message: {len(binary_data)} bytes{Style.RESET_ALL}"
            )
            return

        # Extract event type from first 8 bytes (little-endian format)
        event_type = struct.unpack("<Q", binary_data[:8])[0]
        image_data = binary_data[8:]

        print(
            f"{Fore.LIGHTCYAN_EX}Received preview image: {len(image_data)} bytes (event type: {event_type}){Style.RESET_ALL}"
        )

        # Broadcast the FULL binary data (including header) to Node-RED clients
        await broadcast_to_clients(binary_data, is_binary=True)

    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error handling preview image: {e}{Style.RESET_ALL}")
        print(f"  Binary data length: {len(binary_data)} bytes")


async def execute_workflow(workflow_data):
    """Execute the provided workflow - shared by both modes"""
    global current_prompt_id, ws_connection, session_id, execution_status

    execution_status = "running"

    # Connect to WebSocket first
    ws_connection = None  # Reset the connection
    ws = await connect_comfy_websocket(COMFY_SERVER, COMFY_PORT)
    if not ws:
        execution_status = "error"
        return False

    # Submit the workflow with the session ID as client_id
    api_url = f"http://{COMFY_SERVER}:{COMFY_PORT}/prompt"
    print(f"Submitting workflow to {api_url} with client_id: {session_id}")

    try:
        response = requests.post(
            api_url, json={"prompt": workflow_data, "client_id": session_id}, timeout=30
        )

        if response.status_code != 200:
            print(f"Error submitting workflow: {response.status_code}")
            print(response.text)
            execution_status = "error"
            return False

        result = response.json()
        prompt_id = result.get("prompt_id")
        current_prompt_id = prompt_id

        print(f"Workflow submitted successfully. Prompt ID: {prompt_id}")

        # Check for node errors
        if result.get("node_errors") and len(result.get("node_errors")) > 0:
            print(f"Node errors detected: {result.get('node_errors')}")
            execution_status = "error"
            return False

        # Subscribe to this prompt
        subscribe_msg = {"op": "subscribe_to_prompt", "data": {"prompt_id": prompt_id}}
        await ws_connection.send(json.dumps(subscribe_msg))
        print(f"Subscribed to prompt: {prompt_id}")

        # Monitor for events
        print("Waiting for execution events...")
        execution_complete = False

        try:
            while not execution_complete:
                try:
                    message = await asyncio.wait_for(ws_connection.recv(), timeout=180)

                    # Check if message is binary (preview image data)
                    if isinstance(message, bytes):
                        await handle_preview_image(message)
                        continue

                    # Otherwise, parse as JSON
                    msg_data = json.loads(message)
                    msg_type = msg_data.get("type")

                    # Broadcast these text events to ws clients
                    await broadcast_to_clients(message, is_binary=False)

                    if msg_type != "status":
                        print(f"EVENT: {msg_type}")

                        if msg_type == "progress":
                            value = msg_data.get("data", {}).get("value", 0)
                            max_val = msg_data.get("data", {}).get("max", 100)
                            percent = int((value / max_val) * 100)
                            print(f"  Progress: {value}/{max_val} ({percent}%)")
                        elif msg_type == "executing":
                            node = msg_data.get("data", {}).get("node")
                            print(f"  Executing node: {node}")
                            if str(node) == str(SAVE_IMAGE_NODE_ID):
                                print(
                                    f"{Fore.LIGHTGREEN_EX}Save image node ({SAVE_IMAGE_NODE_ID}) is executing...{Style.RESET_ALL}"
                                )
                        elif msg_type == "executed":
                            node = msg_data.get("data", {}).get("node")
                            if str(node) == str(SAVE_IMAGE_NODE_ID):
                                print(
                                    f"{Fore.LIGHTGREEN_EX}Save image node ({SAVE_IMAGE_NODE_ID}) completed!{Style.RESET_ALL}"
                                )
                                # Wait for file system sync after save node completes
                                await asyncio.sleep(3)
                        elif msg_type in ["execution_success", "execution_complete"]:
                            execution_complete = True
                            print("Workflow execution completed successfully!")
                            break
                        elif msg_type == "execution_error":
                            print(
                                f"Execution error: {msg_data.get('data', {}).get('exception_message', 'Unknown error')}"
                            )
                            execution_status = "error"
                            execution_complete = True
                            break

                except asyncio.TimeoutError:
                    print(
                        f"{Fore.LIGHTYELLOW_EX}WebSocket receiving timed out, but execution may still be running.{Style.RESET_ALL}"
                    )
                    execution_status = "unknown"
                    break
                except websockets.exceptions.ConnectionClosedError as e:
                    print(
                        f"{Fore.LIGHTRED_EX}WebSocket connection closed: {e}{Style.RESET_ALL}"
                    )
                    execution_status = "error"
                    break
                except Exception as e:
                    print(
                        f"{Fore.LIGHTRED_EX}Error receiving message: {e}{Style.RESET_ALL}"
                    )
                    execution_status = "error"
                    break

            await asyncio.sleep(2)  # Allow time for any final messages to be processed
            print(
                "All events processed. Check the output folder for your generated image."
            )
            execution_status = "completed"
            return True

        except Exception as e:
            print(f"{Fore.LIGHTRED_EX}Error monitoring events:{Style.RESET_ALL} {e}")
            execution_status = "error"
            if current_prompt_id:
                cancel_workflow(current_prompt_id)
            return False

    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error submitting workflow:{Style.RESET_ALL} {e}")
        execution_status = "error"
        return False


# HTTP Request Handlers
async def handle_health_check(request):
    """Simple health check endpoint"""
    response_data = {"STATUS": "ComfyUI Workflow Runner is running"}
    return create_json_response(response_data)


async def handle_status(request):
    """Get current execution status and system information"""
    global execution_status, current_prompt_id, workflow_json, connected_clients

    status_data = {
        "STATUS": "Server running",
        "execution_status": execution_status,
        "current_prompt_id": current_prompt_id,
        "workflow_loaded": workflow_json is not None,
        "connected_ws_clients": len(connected_clients),
        "comfy_server": f"{COMFY_SERVER}:{COMFY_PORT}",
        "save_image_node_id": SAVE_IMAGE_NODE_ID,
    }
    return create_json_response(status_data)


async def _execute_workflow_and_get_result():
    """Common workflow execution and result handling"""
    global current_prompt_id

    success = await execute_workflow(workflow_json)

    if success and execution_status == "completed":
        print(
            f"{Fore.LIGHTYELLOW_EX}Workflow completed, getting image info...{Style.RESET_ALL}"
        )

        image_filename, image_url = get_generated_image(current_prompt_id)
        prompt_id_used = current_prompt_id
        current_prompt_id = None

        if image_filename and image_url:
            print(
                f"{Fore.LIGHTGREEN_EX}Generated image: {image_filename}{Style.RESET_ALL}"
            )
            print(f"{Fore.LIGHTCYAN_EX}View at: {image_url}{Style.RESET_ALL}")

            # Also publish to WS
            ws_message = {
                "STATUS": "Workflow completed successfully",
                "type": "image_generated",
                "image_filename": image_filename,
                "image_url": image_url,
                "prompt_id": prompt_id_used,
            }
            # Broadcast these text events to ws clients
            await broadcast_to_clients(json.dumps(ws_message), is_binary=False)

            return {
                "STATUS": "completed successfully",
                "image_filename": image_filename,
                "image_url": image_url,
                "prompt_id": prompt_id_used,
            }, 200
        else:
            print(
                f"{Fore.LIGHTYELLOW_EX}Workflow completed but no image found{Style.RESET_ALL}"
            )
            return {
                "STATUS": "completed but no image found",
                "prompt_id": prompt_id_used,
            }, 500
    else:
        error_status = (
            execution_status if execution_status != "completed" else "unknown_error"
        )
        print(
            f"{Fore.LIGHTRED_EX}Workflow execution failed: {error_status}{Style.RESET_ALL}"
        )
        return {
            "STATUS": f"generation failed - {error_status}",
            "execution_status": execution_status,
        }, 500


async def _validate_workflow_ready():
    """Common workflow readiness validation"""
    if execution_status == "running":
        return {"STATUS": "Workflow is already running"}, 400
    if not workflow_json:
        return {"STATUS": "No workflow loaded"}, 400
    return None, None


async def handle_queue(request):
    """Execute current workflow"""
    error_response, status_code = await _validate_workflow_ready()
    if error_response:
        return create_json_response(error_response, status_code)

    print(f"{Fore.LIGHTCYAN_EX}Received request to execute workflow{Style.RESET_ALL}")

    # OLD
    # result_data, status_code = await _execute_workflow_and_get_result()

    # NEW
    try:
        result_data, status_code = await asyncio.wait_for(
            _execute_workflow_and_get_result(), 
            timeout=300  # 5 minutes max
        )
    except asyncio.TimeoutError:
        return create_json_response({
            "STATUS": "workflow timeout - check /status for completion", 
            "current_prompt_id": current_prompt_id
        }, 408)
    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error executing workflow: {str(e)}{Style.RESET_ALL}")
        return create_json_response({"STATUS": f"Error: {str(e)}"}, 500)
    
    return create_json_response(result_data, status_code)


async def handle_update_text(request):
    """Handle text update requests with node_id and text in JSON body"""
    global workflow_json, execution_status

    if execution_status == "running":
        return create_json_response(
            {"STATUS": "Cannot update text while workflow is running"}, 400
        )

    if not workflow_json:
        return create_json_response({"STATUS": "No workflow loaded"}, 400)

    try:
        # Validate JSON request
        data, error = await validate_json_request(request, ["node_id", "text"])
        if error:
            return create_json_response({"STATUS": error}, 400)

        # Validate node_id
        node_id, error = validate_node_id(data.get("node_id"))
        if error:
            return create_json_response({"STATUS": error}, 400)

        # Update the specific node
        success = update_text_node_with_text(workflow_json, node_id, data.get("text"))

        if success:
            return create_json_response(
                {"STATUS": f"Updated text in node {node_id} successfully"}
            )
        else:
            return create_json_response(
                {"STATUS": f"Failed to update text in node {node_id}"}, 404
            )

    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error updating text: {str(e)}{Style.RESET_ALL}")
        return create_json_response({"STATUS": f"Error updating text: {str(e)}"}, 500)


async def handle_update_image(request):
    """Handle image update requests with node_id and filename in JSON body"""
    global workflow_json, execution_status

    if execution_status == "running":
        return create_json_response(
            {"STATUS": "Cannot update image while workflow is running"}, 400
        )

    if not workflow_json:
        return create_json_response({"STATUS": "No workflow loaded"}, 400)

    try:
        # Validate JSON request
        data, error = await validate_json_request(request, ["node_id", "filename"])
        if error:
            return create_json_response({"STATUS": error}, 400)

        # Validate node_id
        node_id, error = validate_node_id(data.get("node_id"))
        if error:
            return create_json_response({"STATUS": error}, 400)

        # Update the specific node
        success = update_image_node_with_image(
            workflow_json, node_id, data.get("filename")
        )

        if success:
            return create_json_response(
                {
                    "STATUS": f"Updated image in node {node_id} to {data.get('filename')} successfully"
                }
            )
        else:
            return create_json_response(
                {"STATUS": f"Failed to update image in node {node_id}"}, 404
            )

    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error updating image: {str(e)}{Style.RESET_ALL}")
        return create_json_response({"STATUS": f"Error updating image: {str(e)}"}, 500)


async def handle_interrupt(request):
    """Handle interrupt request to stop and CLEAR the current workflow"""
    try:
        # Send response immediately to avoid HTTPie connection issues
        response_data = {"STATUS": "Interrupt request received, processing..."}

        # Create background task for the actual interrupt work
        asyncio.create_task(do_interrupt())

        return create_json_response(response_data)

    except Exception as e:
        print(f"{Fore.LIGHTRED_EX}Error in handle_interrupt: {str(e)}{Style.RESET_ALL}")
        return create_json_response({"STATUS": f"Error: {str(e)}"}, 500)


async def do_interrupt():
    """Actually perform the interrupt operation in background"""
    global current_prompt_id, execution_status

    try:
        # First, interrupt the current execution
        interrupt_url = f"http://{COMFY_SERVER}:{COMFY_PORT}/interrupt"
        print(f"{Fore.LIGHTYELLOW_EX}Interrupting workflow execution{Style.RESET_ALL}")

        async with aiohttp.ClientSession() as session:
            # Send interrupt
            async with session.post(interrupt_url) as interrupt_response:
                if interrupt_response.status != 200:
                    print(
                        f"{Fore.LIGHTRED_EX}Failed to interrupt: {interrupt_response.status}{Style.RESET_ALL}"
                    )
                    return

            # Get current queue
            queue_url = f"http://{COMFY_SERVER}:{COMFY_PORT}/queue"
            async with session.get(queue_url) as queue_response:
                if queue_response.status == 200:
                    queue_data = await queue_response.json()

                    # Collect all prompt IDs to delete
                    queue_running = queue_data.get("queue_running", [])
                    queue_pending = queue_data.get("queue_pending", [])

                    all_prompt_ids = []
                    for item in queue_running + queue_pending:
                        if (
                            len(item) > 1
                        ):  # Queue items are arrays with prompt_id at index 1
                            all_prompt_ids.append(item[1])

                    if all_prompt_ids:
                        # Delete all queued items
                        delete_data = {"delete": all_prompt_ids}
                        async with session.post(
                            queue_url, json=delete_data
                        ) as delete_response:
                            if delete_response.status == 200:
                                print(
                                    f"{Fore.LIGHTGREEN_EX}Successfully cleared {len(all_prompt_ids)} items from queue{Style.RESET_ALL}"
                                )
                            else:
                                print(
                                    f"{Fore.LIGHTRED_EX}Failed to clear queue: {delete_response.status}{Style.RESET_ALL}"
                                )
                    else:
                        print(
                            f"{Fore.LIGHTYELLOW_EX}No items in queue to clear{Style.RESET_ALL}"
                        )
                else:
                    print(
                        f"{Fore.LIGHTRED_EX}Failed to get queue status: {queue_response.status}{Style.RESET_ALL}"
                    )

        # Reset status
        execution_status = "idle"
        current_prompt_id = None
        print(f"{Fore.LIGHTGREEN_EX}Interrupt completed successfully{Style.RESET_ALL}")

    except Exception as e:
        print(
            f"{Fore.LIGHTRED_EX}Error during interrupt operation: {str(e)}{Style.RESET_ALL}"
        )
        execution_status = "error"


# Server Setup Functions
async def start_minimal_http_server():
    """Start a minimal HTTP server with all routes"""
    app = web.Application()

    # Routes - using the correct handler functions
    app.router.add_get("/health", handle_health_check)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/queue", handle_queue)
    app.router.add_post("/update/text", handle_update_text)
    app.router.add_post("/update/image", handle_update_image)
    app.router.add_post("/interrupt", handle_interrupt)

    # Start the server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, COMFY_SERVER, MIDDLEWARE_HTTP_PORT)

    print(
        f"{Fore.LIGHTYELLOW_EX}Starting HTTP server on:{Fore.LIGHTBLACK_EX} http://{COMFY_SERVER}:{MIDDLEWARE_HTTP_PORT} {Style.RESET_ALL}"
    )
    await site.start()

    return runner


async def run_continuous_mode():
    """Simplified: Load workflow → Start server → Wait for API calls"""
    global COMFY_SERVER, COMFY_PORT, workflow_json, ws_connection

    print(
        f"\n{Fore.LIGHTCYAN_EX}Starting ComfyUI workflow executor in {Fore.LIGHTYELLOW_EX}CONTINUOUS{Fore.LIGHTCYAN_EX} mode.{Style.RESET_ALL}"
    )

    # Test connectivity to ComfyUI first
    if not test_comfyui_connection(COMFY_SERVER, COMFY_PORT):
        print(
            f"{Fore.LIGHTRED_EX}Failed to connect to ComfyUI server. Please make sure it's running.{Style.RESET_ALL}"
        )
        return False

    # Load the workflow into memory
    workflow_json = load_workflow_from_file(CURR_WORKFLOW)
    if not workflow_json:
        return False

    # Start HTTP server
    http_runner = await start_minimal_http_server()
    print(
        f"{Fore.LIGHTGREEN_EX}Server is now listening on:{Fore.LIGHTBLACK_EX} http://{COMFY_SERVER}:{MIDDLEWARE_HTTP_PORT} {Style.RESET_ALL}"
    )

    # Start WebSocket server for Node-RED clients
    await start_websocket_server()

    print(
        f"""
    {Fore.LIGHTCYAN_EX}All servers ready!{Style.RESET_ALL}

    {Fore.LIGHTMAGENTA_EX}- HTTP API:{Style.RESET_ALL} http://{COMFY_SERVER}:{MIDDLEWARE_HTTP_PORT}
    {Fore.LIGHTMAGENTA_EX}- WebSocket Stream:{Style.RESET_ALL} ws://{COMFY_SERVER}:{RELAY_WS_PORT} (relay ComfyUI events to clients)

    {Fore.LIGHTCYAN_EX}Available HTTP REST API endpoints:{Style.RESET_ALL}
    - GET  /health         - Health check
    - GET  /status         - System status and information
    - POST /update/text    {{"node_id": 59, "text": "..."}}        - Update text in specific node
    - POST /update/image   {{"node_id": 170, "filename": "..."}}   - Update image in specific node  
    - GET  /queue          - Execute workflow
    - POST /interrupt      - Stop running workflow

    {Fore.LIGHTYELLOW_EX}Workflow loaded and ready for API calls!{Style.RESET_ALL}
    {Fore.LIGHTYELLOW_EX}Ready to relay ComfyUI events to clients (e.g.: Node-RED){Style.RESET_ALL}
    """
    )

    try:
        # Keep the server running and maintain WebSocket connection
        while True:
            try:
                # Periodic WebSocket health check
                if ws_connection is None or ws_connection.closed:
                    print(
                        f"{Fore.YELLOW}WebSocket connection needs refresh. Reconnecting...{Style.RESET_ALL}"
                    )
                    await connect_comfy_websocket(COMFY_SERVER, COMFY_PORT)
            except Exception:
                pass  # Ignore connection check errors
            await asyncio.sleep(30)

    except asyncio.CancelledError:
        print("Server shutdown requested")
    finally:
        print("Cleaning up resources...")
        await http_runner.cleanup()

    return True


def main():
    """Main entry point"""
    print(
        f"{Fore.LIGHTCYAN_EX}ComfyUI Workflow Runner{Style.RESET_ALL} (Press Ctrl+C to cancel at any time)"
    )

    try:
        asyncio.run(run_continuous_mode())
    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected.")
        if current_prompt_id:
            cancel_workflow(current_prompt_id)
    finally:
        print("Script execution complete.")


if __name__ == "__main__":
    main()
