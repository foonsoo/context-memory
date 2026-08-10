import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProcessSmokeTests(unittest.TestCase):
    def env(self):
        value = os.environ.copy(); value["PYTHONPATH"] = str(ROOT / "src"); return value

    def test_real_stdio_server(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = subprocess.Popen([sys.executable, "-m", "context_memory.cli", "--db", str(Path(temp)/"m.db"), "serve"],
                                    cwd=ROOT, env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                request = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
                proc.stdin.write(json.dumps(request) + "\n"); proc.stdin.flush()
                response = json.loads(proc.stdout.readline())
                self.assertEqual(response["result"]["serverInfo"]["name"], "context-memory")
            finally:
                proc.terminate(); proc.wait(timeout=3)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream: stream.close()

    def test_real_http_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            proc = subprocess.Popen([sys.executable, "-m", "context_memory.cli", "--db", str(Path(temp)/"m.db"), "serve", "--transport", "http", "--port", str(port)], cwd=ROOT, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                body = json.dumps({"jsonrpc":"2.0","id":1,"method":"ping"}).encode()
                for _ in range(40):
                    try:
                        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", body, {"Content-Type":"application/json","Accept":"application/json, text/event-stream"})
                        with urllib.request.urlopen(req, timeout=.5) as response: value = json.load(response)
                        break
                    except Exception: time.sleep(.05)
                else: self.fail("HTTP MCP server did not start")
                self.assertEqual(value["result"], {})
            finally:
                proc.terminate(); proc.wait(timeout=3)
                for stream in (proc.stdout, proc.stderr):
                    if stream: stream.close()


if __name__ == "__main__": unittest.main()
