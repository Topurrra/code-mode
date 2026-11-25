# SPDX-License-Identifier: MIT
"""An MCP stdio server that misbehaves on purpose. Standard library only.

It is not used by the application. The end-to-end suite points connectors at
it to see what the runtime does with a server that is slow to start, prints
noise on stdout, answers with CRLF, exits in the middle of a call, returns
text where JSON is expected, publishes an enormous schema, echoes its own
credential, reports progress, honours cancellation, ignores it, ignores the
end of its input, or names two tools so that they collide.

Environment:
  MISBEHAVE_START_DELAY   seconds to wait before answering ``initialize``
  MISBEHAVE_TOKEN         a credential; the ``leak`` tool returns it
  MISBEHAVE_IGNORE_EOF    "1": keep running after stdin closes
  MISBEHAVE_STATE         a folder for files the tests read back
  MISBEHAVE_VERSION       reported by ``version``; lets a test see a restart
  MISBEHAVE_ONE_AT_A_TIME "1": run one tool call at a time, like many simple
                          servers; the others wait in line (a cancellation
                          is still read while they wait)
"""

import json
import os
import subprocess
import sys
import threading
import time

STATE = os.environ.get("MISBEHAVE_STATE") or "."
WRITE_LOCK = threading.Lock()
CALL_LOCK = threading.Lock()  # held by a call when MISBEHAVE_ONE_AT_A_TIME is "1"
CANCELLED = set()
BIG_TEXT = "This parameter is described at great and unnecessary length. " * 12


def note(name, text):
    try:
        with open(os.path.join(STATE, name), "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def send(message):
    data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\r\n"  # CRLF on purpose
    with WRITE_LOCK:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or []}


HUGE = schema({
    "option_%03d" % i: {
        "type": ["string", "integer", "boolean"][i % 3],
        "description": BIG_TEXT,
        "properties": {"nested_%d" % j: {"type": "string", "description": BIG_TEXT} for j in range(3)} if i % 10 == 0 else {},
    }
    for i in range(400)
}, ["option_000"])

TOOLS = [
    {"name": "text", "description": "Return plain text, not JSON.", "inputSchema": schema()},
    {"name": "json_text", "description": "Return a JSON object as a text block.", "inputSchema": schema()},
    {"name": "number_text", "description": "Return the text 42.", "inputSchema": schema()},
    {"name": "multi_text", "description": "Return two text blocks.", "inputSchema": schema()},
    {"name": "image", "description": "Return a text block and an image block.", "inputSchema": schema()},
    {"name": "crash", "description": "Exit in the middle of the call.", "inputSchema": schema()},
    {"name": "pid", "description": "Return this server's process id and how often it was asked.", "inputSchema": schema()},
    {"name": "version", "description": "Return MISBEHAVE_VERSION.", "inputSchema": schema()},
    {"name": "leak", "description": "Return this server's credential, in a result or in an error.",
     "inputSchema": schema({"as_error": {"type": "boolean", "default": False}, "pause": {"type": "number", "default": 0}})},
    {"name": "work", "description": "Wait, then write which process did it and from when to when to a file.",
     "inputSchema": schema({"seconds": {"type": "number"}, "label": {"type": "string"}}, ["seconds", "label"])},
    {"name": "build-step", "description": "The same as work, under a name with a dash.",
     "inputSchema": schema({"seconds": {"type": "number"}, "label": {"type": "string"}}, ["seconds", "label"])},
    {"name": "progress", "description": "Report progress a few times, then finish.",
     "inputSchema": schema({"steps": {"type": "integer", "default": 3}, "pause": {"type": "number", "default": 0.3}})},
    {"name": "slow", "description": "Wait, unless the caller cancels. Writes how it ended to a file.",
     "inputSchema": schema({"seconds": {"type": "number"}, "label": {"type": "string"}}, ["seconds"])},
    {"name": "spawn_child", "description": "Start a child process that sleeps for a minute and return its pid.", "inputSchema": schema()},
    {"name": "stubborn", "description": "Ignore cancellation and hold the server: nothing else is answered until it ends. "
     "Starts a child that starts a sleeping grandchild and exits; the pids go to stubborn-LABEL.json.",
     "inputSchema": schema({"seconds": {"type": "number"}, "label": {"type": "string"}}, ["seconds", "label"])},
    {"name": "running", "description": "Which processes in stubborn-LABEL.json are still running, after waiting up to 5 s for them to end.",
     "inputSchema": schema({"label": {"type": "string"}}, ["label"])},
    {"name": "huge", "description": ("A tool with an enormous schema. " + BIG_TEXT) * 20, "inputSchema": HUGE},
    {"name": "do-it", "description": "One of three names that collide once sanitized.", "inputSchema": schema()},
    {"name": "do_it", "description": "One of three names that collide once sanitized.", "inputSchema": schema()},
    {"name": "do.it", "description": "One of three names that collide once sanitized.", "inputSchema": schema()},
]
ASKED = [0]
GRANDPARENT = (
    "import subprocess, sys\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(g.pid)\n"
)


def running(pid):
    """Is a process with this id running? Standard library only."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            return handle.read().rpartition(b")")[2].split()[:1] != [b"Z"]
    except OSError:
        return True


def text_result(*texts):
    return {"content": [{"type": "text", "text": t} for t in texts]}


def call_tool(request_id, name, args, meta):
    if name == "text":
        return text_result("just some text, with a { brace")
    if name == "json_text":
        return text_result(json.dumps({"ok": True, "items": [1, 2, 3]}))
    if name == "number_text":
        return text_result("42")
    if name == "multi_text":
        return text_result("first block", "second block")
    if name == "image":
        return {"content": [{"type": "text", "text": "a caption"}, {"type": "image", "mimeType": "image/png", "data": "QUJD" * 100}]}
    if name == "crash":
        sys.stderr.write("crashing on purpose in the middle of a call\n")
        sys.stderr.flush()
        os._exit(9)
    if name == "pid":
        ASKED[0] += 1
        return {"content": [], "structuredContent": {"pid": os.getpid(), "asked": ASKED[0]}}
    if name == "version":
        return text_result(os.environ.get("MISBEHAVE_VERSION", "unset"))
    if name == "leak":
        time.sleep(float(args.get("pause", 0)))
        token = os.environ.get("MISBEHAVE_TOKEN", "")
        if args.get("as_error"):
            return {"content": [{"type": "text", "text": "refused; the header was Bearer %s" % token}], "isError": True}
        return {"content": [], "structuredContent": {"token": token, "note": "the token is %s." % token, token: "as a key"}}
    if name == "progress":
        token = (meta or {}).get("progressToken")
        steps = int(args.get("steps", 3))
        for i in range(steps):
            time.sleep(float(args.get("pause", 0.3)))
            if token is not None:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": i + 1, "total": steps, "message": "step %d" % (i + 1)}})
        return {"content": [], "structuredContent": {"steps": steps}}
    if name == "slow":
        label = str(args.get("label", "slow"))
        deadline = time.time() + float(args.get("seconds", 0))
        while time.time() < deadline:
            if request_id in CANCELLED:
                note("slow.log", "%s cancelled" % label)
                return None  # a cancelled request gets no response
            time.sleep(0.05)
        note("slow.log", "%s finished" % label)
        return text_result("slept")
    if name in ("work", "build-step"):
        # The server's own record of when it worked: a test compares these,
        # not the clock of the caller, which also counts the waiting.
        started = time.time()
        time.sleep(float(args.get("seconds", 0)))
        ended = time.time()
        note("work-%d.log" % os.getpid(), json.dumps({"label": args.get("label"), "pid": os.getpid(), "start": started, "end": ended}))
        return {"content": [], "structuredContent": {"label": args.get("label"), "pid": os.getpid()}}
    if name == "spawn_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"content": [], "structuredContent": {"child": child.pid, "server": os.getpid()}}
    if name == "stubborn":
        record = os.path.join(STATE, "stubborn-%s.json" % args["label"])
        # The child exits at once: its grandchild then has no living parent,
        # which is what a build started through a short-lived wrapper is like.
        child = subprocess.Popen([sys.executable, "-c", GRANDPARENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        grandchild = int(child.communicate()[0].strip() or 0)
        with open(record, "w", encoding="utf-8") as handle:
            json.dump({"server": os.getpid(), "grandchild": grandchild}, handle)
        time.sleep(float(args["seconds"]))
        return text_result("stubborn finished")
    if name == "running":
        with open(os.path.join(STATE, "stubborn-%s.json" % args["label"]), encoding="utf-8") as handle:
            pids = json.load(handle)
        deadline = time.time() + 5
        while time.time() < deadline and any(running(pid) for pid in pids.values()):
            time.sleep(0.05)
        return {"content": [], "structuredContent": {role: running(pid) for role, pid in pids.items()}}
    if name in ("do-it", "do_it", "do.it"):
        return text_result("this is %s" % name)
    return {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True}


def handle(message):
    method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "notifications/cancelled":
        CANCELLED.add(params.get("requestId"))
        return
    if request_id is None:
        return
    if method == "initialize":
        time.sleep(float(os.environ.get("MISBEHAVE_START_DELAY", "0")))
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "misbehaving", "version": os.environ.get("MISBEHAVE_VERSION", "1")}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        # Each call on its own thread, so that a slow one can be cancelled.
        def work():
            if os.environ.get("MISBEHAVE_ONE_AT_A_TIME") == "1":
                with CALL_LOCK:
                    result = call_tool(request_id, params.get("name"), params.get("arguments") or {}, params.get("_meta"))
            else:
                result = call_tool(request_id, params.get("name"), params.get("arguments") or {}, params.get("_meta"))
            if result is not None:
                send({"jsonrpc": "2.0", "id": request_id, "result": result})

        if params.get("name") == "stubborn":
            work()  # on the main loop: the server takes one request at a time while this runs
        else:
            threading.Thread(target=work, daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    # Noise on stdout before the first message: a banner, and a line that is
    # JSON but not a protocol message.
    sys.stdout.buffer.write(b"misbehaving server starting up...\r\n{\"level\": \"info\", \"msg\": \"listening\"}\r\n")
    sys.stdout.buffer.flush()
    note("start-%d.log" % os.getpid(), str(os.getpid()))  # a file for each process: two that append to one at the same moment can lose a line
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            handle(json.loads(raw))
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
    if os.environ.get("MISBEHAVE_IGNORE_EOF") == "1":
        while True:  # a server that does not notice its client has gone
            time.sleep(1)


if __name__ == "__main__":
    main()
