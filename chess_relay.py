"""Tool server the model talks to. Runs inside the model's sandbox and only forwards.

It offers two tools, `move` and `bash`, and passes every call over a local
socket to chess_task.py, which plays the move or runs the command in the game container.
Standard library only: it is started with the system Python inside bubblewrap.
"""
import json
import socket
import sys

HOST, PORT, TOKEN = sys.argv[1], int(sys.argv[2]), sys.argv[3]
TOOLS = [
    {"name": "move",
     "description": "Play your next move as White, in standard notation (for example e4, Nf3, O-O) or as "
                    "squares (for example e2e4). The bot answers at once and the new position is returned.",
     "inputSchema": {"type": "object", "properties": {"move": {"type": "string"}}, "required": ["move"]}},
    {"name": "bash",
     "description": "Run a bash command on the game machine and return its output.",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]


def forward(name, arguments):
    with socket.create_connection((HOST, PORT)) as s:
        s.sendall((json.dumps({"token": TOKEN, "name": name, "arguments": arguments}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)["text"]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        m = json.loads(line)
    except json.JSONDecodeError:
        continue
    mid, method = m.get("id"), m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": m["params"]["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "box", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        try:
            text = forward(m["params"]["name"], m["params"].get("arguments") or {})
        except Exception as e:  # noqa: BLE001
            text = f"tool error: {e}"
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
