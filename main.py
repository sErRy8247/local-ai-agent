from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx, json, re
from pathlib import Path
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LM_STUDIO_URL  = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_PING = "http://localhost:1234/v1/models"
MODEL_NAME     = "qwen3.6-35b-a3b"

# ── AGENT.md 하네스 로더 ─────────────────────────────────────
HARNESS_PATH = Path(__file__).parent / "AGENT.md"

def load_harness() -> str:
    if HARNESS_PATH.exists():
        return HARNESS_PATH.read_text(encoding="utf-8")
    return (
        "You are a helpful AI assistant. "
        "Always reply in the user's language. "
        "[WARNING] AGENT.md harness file not found."
    )

# ── 대화 저장 디렉터리 ────────────────────────────────────────
CONVERSATIONS_DIR = Path(__file__).parent / "conversations"
CONVERSATIONS_DIR.mkdir(exist_ok=True)

# ── Think 블록 정리 ───────────────────────────────────────────
_THINK_RE = re.compile(
    r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>|<think(?:ing)?>[\s\S]*$',
    re.IGNORECASE
)

def clean_think_blocks(content: str) -> str:
    return _THINK_RE.sub('', content).strip()

# ── Pydantic 모델 ────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[Message]
    enable_thinking: bool = True

# ── 대화 저장 모델 ────────────────────────────────────────────
class DisplayMsg(BaseModel):
    role: str        # 'user' | 'ai'
    text: str        # 원본 텍스트 (user: 입력값, ai: fullText)
    time: str        # "PM 10:41"
    think: bool = False  # AI 메시지의 Think 모드 여부

class ConvSaveRequest(BaseModel):
    id: str
    title: str
    messages: list[Message]   # API 히스토리 (user+assistant)
    display: list[DisplayMsg] # UI 표시용

# ── 헬퍼 함수 ─────────────────────────────────────────────────
def _safe_id(conv_id: str) -> str:
    """경로 순회 방지: 영숫자·언더스코어·하이픈만 허용"""
    return re.sub(r'[^\w\-]', '', conv_id)

def _conv_path(conv_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{_safe_id(conv_id)}.md"

def _build_md(req: ConvSaveRequest) -> str:
    """
    사람이 읽을 수 있는 .md 본문 + JSON 데이터 블록 생성.
    Obsidian에서 열면 대화 내용이 깔끔하게 보임.
    <!-- CONV_DATA --> 블록은 렌더링 시 숨겨지며 앱 로딩 시 사용.
    """
    lines = [
        "---",
        f"id: {req.id}",
        f"title: {req.title}",
        f"created: {datetime.now().isoformat()}",
        f"model: {MODEL_NAME}",
        "---",
        "",
        f"# {req.title}",
        "",
    ]

    for msg in req.display:
        if msg.role == "user":
            lines += [f"**나** `{msg.time}`", "", msg.text, "", "---", ""]
        else:
            mode_tag = "🧠 Think" if msg.think else "⚡ Fast"
            lines += [f"**AI** `{msg.time}` `{mode_tag}`", ""]
            # AI 응답 본문 (think 블록 제거된 버전만 표시)
            clean = clean_think_blocks(msg.text)
            lines += [clean if clean else "*(응답 없음)*", "", "---", ""]

    # JSON 데이터 블록 (HTML 주석 → 렌더링 시 숨겨짐, 앱 로딩 시 파싱)
    data = {
        "messages": [m.dict() for m in req.messages],
        "display":  [d.dict() for d in req.display],
    }
    lines += [
        "<!-- CONV_DATA",
        json.dumps(data, ensure_ascii=False),
        "-->",
    ]

    return "\n".join(lines)

def _parse_md_meta(content: str) -> dict:
    """YAML frontmatter 파싱."""
    meta = {}
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta

# ── 라우트: HTML 서빙 ────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(
        "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
            "Expires":       "0",
        }
    )

# ── 라우트: 헬스체크 ─────────────────────────────────────────
@app.get("/health")
async def health():
    lm_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(LM_STUDIO_PING)
            lm_ok = r.status_code == 200
    except Exception:
        lm_ok = False
    return JSONResponse({
        "fastapi":   True,
        "lm_studio": lm_ok,
        "harness":   HARNESS_PATH.name if HARNESS_PATH.exists() else None,
    })

# ── 라우트: 대화 저장 ─────────────────────────────────────────
@app.post("/conversations")
async def save_conversation(req: ConvSaveRequest):
    content = _build_md(req)
    _conv_path(req.id).write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True, "id": req.id})

# ── 라우트: 대화 목록 ─────────────────────────────────────────
@app.get("/conversations")
async def list_conversations():
    convs = []
    for f in sorted(
        CONVERSATIONS_DIR.glob("*.md"),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    ):
        try:
            content = f.read_text(encoding="utf-8")
            meta    = _parse_md_meta(content)
            convs.append({
                "id":      meta.get("id", f.stem),
                "title":   meta.get("title", f.stem),
                "created": meta.get("created", ""),
                "mtime":   f.stat().st_mtime,
            })
        except Exception:
            continue
    return JSONResponse({"conversations": convs})

# ── 라우트: 대화 불러오기 ─────────────────────────────────────
@app.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    path = _conv_path(conv_id)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    content = path.read_text(encoding="utf-8")
    meta    = _parse_md_meta(content)

    # CONV_DATA 블록 파싱
    data       = {}
    data_match = re.search(r'<!-- CONV_DATA\n(.*?)\n-->', content, re.DOTALL)
    if data_match:
        try:
            data = json.loads(data_match.group(1))
        except Exception:
            pass

    return JSONResponse({
        "id":       meta.get("id", conv_id),
        "title":    meta.get("title", conv_id),
        "created":  meta.get("created", ""),
        "messages": data.get("messages", []),
        "display":  data.get("display", []),
    })

# ── 라우트: 대화 삭제 ─────────────────────────────────────────
@app.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    path = _conv_path(conv_id)
    if path.exists():
        path.unlink()
    return JSONResponse({"ok": True})

# ── 라우트: 채팅 스트리밍 ────────────────────────────────────
@app.post("/chat")
async def chat(request: ChatRequest):
    system_prompt = load_harness()
    messages = [m for m in request.messages if m.role != "system"]

    # Think/Fast 접두어 삽입
    if messages and messages[-1].role == "user":
        raw = messages[-1].content
        for prefix in ("/think ", "/no_think "):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        injected = "/think " if request.enable_thinking else "/no_think "
        messages[-1] = Message(role="user", content=injected + raw)

    # 히스토리 think 블록 제거 (컨텍스트 오염 방지)
    final_messages = [
        {"role": "system", "content": system_prompt},
        *[
            {
                "role": m.role,
                "content": (
                    clean_think_blocks(m.content)
                    if m.role == "assistant"
                    else m.content
                ),
            }
            for m in messages
        ],
    ]

    async def generate():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", LM_STUDIO_URL, json={
                "model":              MODEL_NAME,
                "messages":           final_messages,
                "stream":             True,
                "temperature":        0.7,
                "top_p":              0.9,
                "top_k":              20,
                "repetition_penalty": 1.05,
                "max_tokens":         4096,
                "enable_thinking":    request.enable_thinking,
            }) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])
                            if content := data["choices"][0]["delta"].get("content"):
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except (json.JSONDecodeError, KeyError):
                            continue

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── 라우트: 하네스 확인 (개발용) ─────────────────────────────
@app.get("/harness")
async def get_harness():
    return JSONResponse({
        "path":    str(HARNESS_PATH),
        "exists":  HARNESS_PATH.exists(),
        "content": load_harness(),
    })
