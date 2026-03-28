import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from google import genai
from google.genai import types


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

if not PROJECT_ID:
    raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")

client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
)

DEFAULT_MODEL = os.getenv("PAGE_AGENT_MODEL", "gemini-3.1-flash-lite-preview")
GENERALIST_MODEL = os.getenv("PAGE_AGENT_GENERALIST_MODEL", "gemini-3.1-pro-preview")


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class Element(BaseModel):
    agent_id: str
    tag: str
    role: str = ""
    text: str = ""
    aria_label: str = ""
    placeholder: str = ""
    href: Optional[str] = None
    input_type: Optional[str] = None
    disabled: bool = False
    bbox: Optional[BBox] = None


class PageContext(BaseModel):
    url: str
    title: str
    selected_text: str = ""
    viewport_text: List[str] = Field(default_factory=list)
    elements: List[Element] = Field(default_factory=list)
    screenshot_data_url: Optional[str] = None
    dom_version: int = 0


class TurnRequest(BaseModel):
    session_id: str
    user_text: str
    page_context: PageContext


class BrowserCommand(BaseModel):
    kind: str
    target_agent_id: Optional[str] = None
    text: Optional[str] = None
    reason: Optional[str] = None


class TurnResponse(BaseModel):
    assistant_text: str
    commands: List[BrowserCommand] = Field(default_factory=list)
    requires_confirmation: bool = False


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

app = FastAPI(title="page-agent-minimal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_KINDS = {"highlight", "scroll", "click", "type"}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def best_matching_element(user_text: str, elements: List[Element]) -> Optional[Element]:
    q = set(tokenize(user_text))
    if not q:
        return None

    best = None
    best_score = -1
    for el in elements:
        hay = " ".join([
            el.text or "",
            el.aria_label or "",
            el.placeholder or "",
            el.tag or "",
            el.role or "",
        ]).lower()
        score = sum(1 for tok in q if tok in hay)
        if score > best_score and not el.disabled:
            best_score = score
            best = el

    return best if best_score > 0 else None


def extract_type_text(user_text: str) -> Optional[str]:
    m = re.search(r'"([^"]+)"', user_text)
    if m:
        return m.group(1)
    m = re.search(r"'([^']+)'", user_text)
    if m:
        return m.group(1)
    return None


def deterministic_fallback(req: TurnRequest) -> TurnResponse:
    user = req.user_text.lower()
    elements = req.page_context.elements

    best = best_matching_element(req.user_text, elements)

    if "type" in user or "fill" in user or "enter" in user:
        text_value = extract_type_text(req.user_text) or "demo text"
        input_el = next(
            (e for e in elements if e.tag in {"input", "textarea"} and not e.disabled),
            None,
        )
        if input_el:
            return TurnResponse(
                assistant_text=f'I found an input field and can type "{text_value}".',
                commands=[
                    BrowserCommand(kind="highlight", target_agent_id=input_el.agent_id),
                    BrowserCommand(kind="type", target_agent_id=input_el.agent_id, text=text_value),
                ],
                requires_confirmation=True,
            )

    if best and any(k in user for k in ["click", "open", "press"]):
        return TurnResponse(
            assistant_text=f'I found a likely target: "{best.text or best.aria_label or best.agent_id}".',
            commands=[
                BrowserCommand(kind="highlight", target_agent_id=best.agent_id),
                BrowserCommand(kind="click", target_agent_id=best.agent_id),
            ],
            requires_confirmation=True,
        )

    if best and any(k in user for k in ["find", "show", "go", "scroll", "pricing", "plan"]):
        return TurnResponse(
            assistant_text=f'I found a likely match: "{best.text or best.aria_label or best.agent_id}".',
            commands=[
                BrowserCommand(kind="highlight", target_agent_id=best.agent_id),
                BrowserCommand(kind="scroll", target_agent_id=best.agent_id),
            ],
            requires_confirmation=False,
        )

    summary = req.page_context.viewport_text[:3]
    if summary:
        return TurnResponse(
            assistant_text="I could not map that to a precise page action.\n\nCurrent page summary:\n- " + "\n- ".join(summary),
            commands=[],
            requires_confirmation=False,
        )

    return TurnResponse(
        assistant_text="I could not infer a safe page action.",
        commands=[],
        requires_confirmation=False,
    )


def validate_commands(commands: List[Dict[str, Any]], valid_ids: set[str]) -> List[BrowserCommand]:
    out: List[BrowserCommand] = []

    for cmd in commands:
        kind = (cmd.get("kind") or "").strip().lower()
        target_agent_id = cmd.get("target_agent_id")
        text = cmd.get("text")
        reason = cmd.get("reason")

        if kind not in ALLOWED_KINDS:
            continue

        if kind in {"highlight", "scroll", "click", "type"}:
            if not target_agent_id or target_agent_id not in valid_ids:
                continue

        if kind == "type" and not isinstance(text, str):
            continue

        out.append(
            BrowserCommand(
                kind=kind,
                target_agent_id=target_agent_id,
                text=text,
                reason=reason,
            )
        )
    return out


def choose_model(user_text: str) -> str:
    lower = user_text.lower()
    if any(k in lower for k in ["compare", "plan", "workflow", "step by step", "deeply", "analyze"]):
        return GENERALIST_MODEL
    return DEFAULT_MODEL


def call_vertex(req: TurnRequest) -> TurnResponse:
    valid_ids = {e.agent_id for e in req.page_context.elements}

    prompt = f"""
You are the browser-page specialist for a Chrome extension.

Return STRICT JSON only.
No markdown.
No explanation outside JSON.

Schema:
{{
  "assistant_text": "string",
  "requires_confirmation": true_or_false,
  "commands": [
    {{
      "kind": "highlight|scroll|click|type",
      "target_agent_id": "existing agent_id or null",
      "text": "string or null",
      "reason": "short reason"
    }}
  ]
}}

Rules:
- Use ONLY the provided agent_ids.
- Prefer the smallest safe action.
- For requests like "find/show/go/pricing", use highlight + scroll.
- For click/type, set requires_confirmation=true.
- If unsure, return no commands and explain briefly.

User request:
{req.user_text}

Page:
URL: {req.page_context.url}
Title: {req.page_context.title}
Selected text: {req.page_context.selected_text}

Visible text:
{chr(10).join(req.page_context.viewport_text[:15])}

Elements:
{json.dumps([e.model_dump() for e in req.page_context.elements[:120]], ensure_ascii=False)}
""".strip()

    response = client.models.generate_content(
        model=choose_model(req.user_text),
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw = strip_fences(response.text or "")
    data = json.loads(raw)

    commands = validate_commands(data.get("commands", []), valid_ids)
    assistant_text = data.get("assistant_text", "Done.")
    requires_confirmation = bool(data.get("requires_confirmation", False))

    return TurnResponse(
        assistant_text=assistant_text,
        commands=commands,
        requires_confirmation=requires_confirmation,
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"ok": "true"}


@app.post("/turn", response_model=TurnResponse)
def turn(req: TurnRequest) -> TurnResponse:
    try:
        return call_vertex(req)
    except Exception as e:
        print(f"Vertex call failed, using fallback: {e}")
        return deterministic_fallback(req)