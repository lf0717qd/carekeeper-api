# -*- coding: utf-8 -*-
"""乡音健康管家 V1.1 Demo 后端。

- 静态文件：index.html / styles.css / app.js / 截图
- GET  /api/health  ：返回 LLM 是否就绪（不暴露 key）
- POST /api/chat    ：粤语健康问答；红线先走规则，再走 LLM
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8766"))


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 优先读 demo/.env；本地演示回退到 agent-lab/.env；不打印 key
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent.parent / "agent-lab" / ".env")

BASE_URL = os.getenv("BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
MODEL = os.getenv("MODEL", "glm-4-flash")
API_KEY = os.getenv("API_KEY", "")
ASR_MODEL = os.getenv("ASR_MODEL", "glm-asr-2512")
ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"

RED_FLAGS = [
    "胸痛", "胸口痛", "心口痛", "心口闷", "胸闷", "唞唔到气", "呼吸困难",
    "出冷汗", "昏迷", "晕倒", "意识", "讲唔清", "半身", "偏瘫", "大出血",
    "跌倒", "摔", "低血糖", "抽搐",
]

SYSTEM_PROMPT = """你系“乡音健康管家”，服务广州广府片嘅居家老人。你只做健康教育、就医引导、用药提醒同复诊提醒，唔做诊断、唔开处方、唔调整药物剂量。

必须遵守：
1. 用粤语口语回答，称呼“婆婆/阿伯”，短句、慢速、先讲结论，最多 120 个中文字。
2. 遇到胸痛、呼吸困难、意识改变、跌倒、低血糖、大出血等红线：risk=high，needs_human=true，回复要叫对方坐低/唔好行，并叫家人/120，唔好自行外出。
3. 药物问题只解释标签、提醒、漏服；强调“唔好自己加量/补双倍”，建议问医生/药师；唔好给具体剂量调整。
4. 唔确定、超出范围、涉及诊断/处方：needs_human=true，建议转家人/家医/人工，唔好硬答。
5. 唔编造政策、药物剂量、检查结果；唔好讲“你得了XX病”。
6. 回答要让人听懂，避免医学术语；必须提醒“呢个只系健康教育，唔系诊断”。
7. 先直接回答用户问题，再给 1-2 条可执行建议；饮食、作息、监测等一般性生活建议可以直接讲（例如份量、搭配、食后测血糖），唔好每次都只讲“问医生”。
8. 饮食问题唔好讲“无问题/随便食”，要讲“可以，但要控制份量/搭配/监测”。
9. 只有涉及诊断、处方、剂量调整、检查结果解释时，才转医生/药师；一般健康教育要给出有用信息。

只输出 JSON，不要 markdown，不要解释 JSON：
{"reply":"粤语口语回答","intent":"用药/血压/复诊/症状/其他","risk":"low|medium|high","needs_human":false,"tool_calls":["memory.get_profile"],"follow_up":"下一步建议"}
"""


STREAM_SYSTEM_PROMPT = """你系“乡音健康管家”，服务广州广府片嘅居家老人。你只做健康教育、就医引导、用药提醒同复诊提醒，唔做诊断、唔开处方、唔调整药物剂量。

必须遵守：
1. 只输出一段粤语口语回答，不要 JSON、不要 markdown、不要标题，最多 120 个中文字。
2. 称呼“婆婆/阿伯”，短句、慢速、先讲结论；要让人听得懂。
3. 遇到胸痛、呼吸困难、意识改变、跌倒、低血糖、大出血等红线：叫对方坐低/唔好行，并叫家人/120；唔好自行外出。
4. 药物问题只解释标签、提醒、漏服；强调“唔好自己加量/补双倍”，建议问医生/药师；唔好给剂量调整。
5. 唔确定、涉及诊断/处方：建议转家人/家医/人工，唔好硬答。
6. 饮食问题唔好讲“无问题/随便食”，要讲“可以，但要控制份量/搭配/监测”。
7. 最后必须加一句“呢个只系健康教育，唔系诊断。”
"""

def has_red_flag(text: str) -> bool:
    return any(word in text for word in RED_FLAGS)


def extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^\x60{3}(?:json)?", "", text).strip()
    text = re.sub(r"\x60{3}$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def fallback(message: str, reason: str = "") -> dict:
    if has_red_flag(message):
        reply = "婆婆，你而家唔好行，坐低或者半躺。我帮你叫家人同 120。你保持同我讲嘢，唔好自己走出去。"
        return {
            "reply": reply,
            "intent": "红线",
            "risk": "high",
            "needs_human": True,
            "tool_calls": ["triage.red_flag", "escalate.120", "notify.family"],
            "follow_up": "保持在线，等家人/救护车；不要自行外出。",
            "source": "rule",
            "reason": reason,
        }
    return {
        "reply": "婆婆，我而家未系好听得明。你可以讲多次，或者我帮你叫家人/姑娘。",
        "intent": "未识别",
        "risk": "low",
        "needs_human": True,
        "tool_calls": ["human.takeover"],
        "follow_up": "转家人/人工确认。",
        "source": "fallback",
        "reason": reason,
    }


def ask_llm(message: str, profile: dict, history: list[dict]) -> dict:
    if not API_KEY:
        return fallback(message, "API_KEY 未配置")

    # 红线不交给 LLM，直接走规则
    if has_red_flag(message):
        return fallback(message, "红线规则命中")

    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=25)
    recent = []
    for item in (history or [])[-6:]:
        role = item.get("role")
        content = str(item.get("content", ""))[:400]
        if role in ("user", "assistant") and content:
            recent.append({"role": role, "content": content})

    user_payload = json.dumps(
        {"message": message, "profile": profile, "recent": recent},
        ensure_ascii=False,
    )
    started = time.time()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        temperature=0.25,
        max_tokens=320,
    )
    raw = response.choices[0].message.content or ""
    data = extract_json(raw)
    elapsed = round((time.time() - started) * 1000)

    if not data or not data.get("reply"):
        result = fallback(message, "LLM 返回非 JSON")
        result["raw"] = raw[:500]
        result["latency_ms"] = elapsed
        result["source"] = "llm_parse_fallback"
        return result

    # 服务端兜底：模型说 high risk，必须人工；模型漏了红线，规则补上
    result = {
        "reply": str(data.get("reply", ""))[:500],
        "intent": str(data.get("intent", "其他"))[:40],
        "risk": str(data.get("risk", "low"))[:10],
        "needs_human": bool(data.get("needs_human", False)),
        "tool_calls": data.get("tool_calls", []) if isinstance(data.get("tool_calls"), list) else [],
        "follow_up": str(data.get("follow_up", ""))[:200],
        "source": "llm",
        "model": MODEL,
        "latency_ms": elapsed,
    }
    if has_red_flag(message):
        result["risk"] = "high"
        result["needs_human"] = True
        result["tool_calls"] = list(dict.fromkeys(result["tool_calls"] + ["triage.red_flag", "escalate.120"]))
    if "唔系诊断" not in result["reply"] and "不是诊断" not in result["reply"]:
        result["reply"] = result["reply"].rstrip("。") + "。呢个只系健康教育，唔系诊断。"
    return result


def stream_llm(message: str, profile: dict, history: list[dict]):
    """SSE 流式回答：非红线走 LLM，红线/无 key 走规则。"""
    started = time.time()
    if not API_KEY or has_red_flag(message):
        data = fallback(message, "流式规则兜底")
        yield "meta", {"source": data["source"], "model": MODEL, "risk": data["risk"], "needs_human": data["needs_human"]}
        yield "delta", {"text": data["reply"]}
        yield "done", {
            "source": data["source"],
            "risk": data["risk"],
            "needs_human": data["needs_human"],
            "intent": data["intent"],
            "latency_ms": round((time.time() - started) * 1000),
        }
        return

    from openai import OpenAI

    recent = []
    for item in (history or [])[-6:]:
        role = item.get("role")
        content = str(item.get("content", ""))[:400]
        if role in ("user", "assistant") and content:
            recent.append({"role": role, "content": content})

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=30)
    payload = json.dumps({"message": message, "profile": profile, "recent": recent}, ensure_ascii=False)
    full = ""
    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": STREAM_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=0.25,
            max_tokens=260,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                full += delta
                yield "delta", {"text": delta}
    except Exception as exc:  # noqa: BLE001
        print("[stream] error:", repr(exc), file=sys.stderr)
        yield "error", {"message": "LLM 流式调用失败：" + type(exc).__name__}
        return

    full = full.strip()
    if not full:
        yield "error", {"message": "LLM 没有返回内容"}
        return

    if "唔系诊断" not in full and "不是诊断" not in full:
        suffix = "呢个只系健康教育，唔系诊断。"
        full += suffix
        yield "suffix", {"text": suffix}

    yield "done", {
        "source": "llm_stream",
        "model": MODEL,
        "risk": "low",
        "needs_human": False,
        "intent": "自由问答",
        "latency_ms": round((time.time() - started) * 1000),
    }


def transcribe(audio_base64: str) -> dict:
    if not API_KEY:
        raise RuntimeError("API_KEY 未配置")
    if not audio_base64:
        raise ValueError("audio_base64 为空")
    raw = base64.b64decode(audio_base64, validate=False)
    if len(raw) > 25 * 1024 * 1024:
        raise ValueError("音频超过 25MB")
    body = json.dumps({
        "model": ASR_MODEL,
        "file_base64": audio_base64,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        ASR_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {
        "text": str(payload.get("text", "")).strip(),
        "model": ASR_MODEL,
        "latency_ms": round((time.time() - started) * 1000),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(200, {
                "ok": bool(API_KEY),
                "model": MODEL,
                "base_url": BASE_URL,
                "has_key": bool(API_KEY),
            })
            return
        return super().do_GET()

    def _stream(self, payload: dict) -> None:
        message = str(payload.get("message", "")).strip()
        profile = payload.get("profile") or {}
        history = payload.get("history") or []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event, data in stream_llm(message, profile, history):
                chunk = "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("[stream] client disconnected", file=sys.stderr)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/chat_stream", "/api/asr"):
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/asr":
                result = transcribe(str(payload.get("audio_base64", "")))
                self._json(200, result)
                return
            message = str(payload.get("message", "")).strip()
            if not message:
                self._json(400, {"error": "message required"})
                return
            if path == "/api/chat_stream":
                self._stream(payload)
                return
            profile = payload.get("profile") or {}
            history = payload.get("history") or []
            result = ask_llm(message, profile, history)
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            print("[api] error:", repr(exc), file=sys.stderr)
            self._json(200, fallback("", "服务异常: " + type(exc).__name__))

    def log_message(self, fmt: str, *args) -> None:
        print("[http] " + fmt % args, file=sys.stderr)


def main() -> None:
    print(f"[boot] {ROOT}")
    print(f"[boot] model={MODEL} base={BASE_URL} key={'set' if API_KEY else 'missing'}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[boot] http://{HOST}:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
