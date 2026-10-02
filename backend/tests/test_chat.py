import os, sys
from pathlib import Path
os.environ["LLM_MODE"] = "echo"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health():
    assert client.get("/health").json()["status"] == "ok"

def test_echo():
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["reply"] == "(echo) You said: hi"

def test_rejects_empty():
    assert client.post("/api/chat", json={"messages": []}).status_code == 422

def test_rejects_last_turn_from_assistant():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert client.post("/api/chat", json={"messages": msgs}).status_code == 400
