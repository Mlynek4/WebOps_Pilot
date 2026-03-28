import base64
import json
import logging
import os
import re
from io import BytesIO
from threading import Lock
from time import time
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pypdf import PdfReader

from google import genai
from google.genai import types


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

logger = logging.getLogger("page-agent")
logging.basicConfig(level=logging.INFO)

DEFAULT_MODEL = os.getenv("PAGE_AGENT_MODEL", "gemini-3.1-flash-lite-preview")
GENERALIST_MODEL = os.getenv("PAGE_AGENT_GENERALIST_MODEL", "gemini-3.1-pro-preview")
ROUTER_MODEL = os.getenv("PAGE_AGENT_ROUTER_MODEL", DEFAULT_MODEL)
VISION_MODEL = os.getenv("PAGE_AGENT_VISION_MODEL", "gemini-2.5-flash-lite")
SPECIALIST_MODEL = os.getenv("PAGE_AGENT_SPECIALIST_MODEL", VISION_MODEL)
MAX_SESSION_HISTORY = int(os.getenv("PAGE_AGENT_MAX_SESSION_HISTORY", "8"))
MAX_GROUNDED_CANDIDATES = int(os.getenv("PAGE_AGENT_MAX_GROUNDED_CANDIDATES", "20"))
GENERIC_TARGET_QUERIES = {
    "pricing",
    "price",
    "plans",
    "plan",
    "login",
    "log in",
    "sign in",
    "signin",
    "download",
    "contact",
    "support",
    "help",
    "share",
    "article",
    "video",
    "paper",
}

_client: Optional[genai.Client] = None

PDF_MAX_BYTES = 15 * 1024 * 1024
PDF_MAX_PAGES = 8
PDF_TEXT_CHAR_LIMIT = 12000


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class ViewportMetrics(BaseModel):
    width: float
    height: float
    page_width: float = 0
    page_height: float = 0
    max_scroll_y: float = 0
    scroll_x: float = 0
    scroll_y: float = 0
    device_pixel_ratio: float = 1.0


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
    page_bbox: Optional[BBox] = None


class ViewportCapture(BaseModel):
    index: int
    scroll_y: float = 0
    viewport: Optional[ViewportMetrics] = None
    viewport_text: List[str] = Field(default_factory=list)
    elements: List[Element] = Field(default_factory=list)
    screenshot_data_url: Optional[str] = None


class PageContext(BaseModel):
    url: str
    title: str
    selected_text: str = ""
    viewport_text: List[str] = Field(default_factory=list)
    elements: List[Element] = Field(default_factory=list)
    screenshot_data_url: Optional[str] = None
    viewport_captures: List[ViewportCapture] = Field(default_factory=list)
    viewport: Optional[ViewportMetrics] = None
    capture_mode: str = "viewport"
    scan_complete: bool = True
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
    direction: Optional[str] = None
    amount: Optional[float] = None


class CandidateMatch(BaseModel):
    agent_id: str
    label: str = ""
    reason: str = ""
    score: float = 0.0


class TurnResponse(BaseModel):
    assistant_text: str
    candidates: List[CandidateMatch] = Field(default_factory=list)
    commands: List[BrowserCommand] = Field(default_factory=list)
    requires_confirmation: bool = False


class SearchIntent(BaseModel):
    mode: str = "general"
    target_query: str = ""
    target_synonyms: List[str] = Field(default_factory=list)
    wants_all_occurrences: bool = False
    wants_navigation: bool = False
    reason: str = ""


class SessionState(BaseModel):
    last_user_text: str = ""
    last_assistant_text: str = ""
    last_url: str = ""
    last_title: str = ""
    last_intent: Optional[SearchIntent] = None
    last_candidates: List[CandidateMatch] = Field(default_factory=list)
    history: List[str] = Field(default_factory=list)
    updated_at: float = 0.0


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

app = FastAPI(title="page-agent-minimal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_KINDS = {"highlight", "scroll", "scroll_page", "click", "type"}
_session_lock = Lock()
_session_store: Dict[str, SessionState] = {}


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


def element_label(element: Element) -> str:
    return (
        element.text
        or element.aria_label
        or element.placeholder
        or element.href
        or element.agent_id
    )


def is_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    return path.endswith(".pdf") or "/pdf/" in path


def fetch_pdf_excerpt(url: str) -> Optional[str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Page-Agent/0.1 (+https://localhost)"
        },
    )

    try:
        with urlopen(request, timeout=20) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "pdf" not in content_type and not is_pdf_url(url):
                return None

            data = response.read(PDF_MAX_BYTES + 1)
    except (URLError, TimeoutError, ValueError) as exc:
        logger.warning("PDF fetch failed for %s: %s", url, exc)
        return None

    if len(data) > PDF_MAX_BYTES:
        logger.warning("Skipping PDF larger than %s bytes: %s", PDF_MAX_BYTES, url)
        return None

    try:
        reader = PdfReader(BytesIO(data))
        pages = []
        for page in reader.pages[:PDF_MAX_PAGES]:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
            if sum(len(item) for item in pages) >= PDF_TEXT_CHAR_LIMIT:
                break
    except Exception as exc:
        logger.warning("PDF parse failed for %s: %s", url, exc)
        return None

    if not pages:
        return None

    excerpt = "\n".join(pages)
    excerpt = re.sub(r"\n{3,}", "\n\n", excerpt)
    return excerpt[:PDF_TEXT_CHAR_LIMIT].strip()


def enrich_page_context(page_context: PageContext) -> PageContext:
    visible_text = " ".join(page_context.viewport_text).strip()
    if len(visible_text) >= 500 or not is_pdf_url(page_context.url):
        return page_context

    pdf_excerpt = fetch_pdf_excerpt(page_context.url)
    if not pdf_excerpt:
        return page_context

    pdf_lines = [
        line.strip()
        for line in pdf_excerpt.splitlines()
        if line.strip()
    ]

    if not pdf_lines:
        return page_context

    return page_context.model_copy(
        update={
            "viewport_text": pdf_lines[:30]
        }
    )


def screenshot_part(data_url: Optional[str]) -> Optional[types.Part]:
    if not data_url or "," not in data_url:
        return None

    header, encoded = data_url.split(",", 1)
    if not header.startswith("data:"):
        return None

    mime_type = header.split(";", 1)[0].split(":", 1)[1]
    try:
        raw = base64.b64decode(encoded)
    except ValueError:
        return None

    return types.Part.from_bytes(data=raw, mime_type=mime_type)


def page_sort_key(element: Element) -> tuple[float, float, str]:
    if element.page_bbox is not None:
        return (element.page_bbox.y, element.page_bbox.x, element.agent_id)
    if element.bbox is not None:
        return (element.bbox.y, element.bbox.x, element.agent_id)
    return (float("inf"), float("inf"), element.agent_id)


def build_capture_payload(capture: ViewportCapture) -> Dict[str, Any]:
    return {
        "index": capture.index,
        "scroll_y": capture.scroll_y,
        "viewport": capture.viewport.model_dump() if capture.viewport else None,
        "viewport_text": capture.viewport_text[:12],
        "elements": [element.model_dump() for element in capture.elements[:80]],
    }


def build_multimodal_parts(req: TurnRequest, prompt: str) -> List[types.Part]:
    parts: List[types.Part] = [types.Part.from_text(text=prompt)]

    if req.page_context.viewport_captures:
        for capture in req.page_context.viewport_captures:
            parts.append(
                types.Part.from_text(
                    text=(
                        f"Viewport capture {capture.index + 1} metadata:\n"
                        f"{json.dumps(build_capture_payload(capture), ensure_ascii=False)}"
                    )
                )
            )
            image_part = screenshot_part(capture.screenshot_data_url)
            if image_part is not None:
                parts.append(image_part)
        return parts

    image_part = screenshot_part(req.page_context.screenshot_data_url)
    if image_part is not None:
        parts.append(image_part)
    return parts


def get_client() -> genai.Client:
    global _client

    if _client is not None:
        return _client

    try:
        _client = genai.Client()
        logger.info("google-genai client initialized using environment configuration")
        return _client
    except Exception as exc:
        runtime_error = RuntimeError(
            "Unable to initialize google-genai client. "
            "Set GOOGLE_GENAI_USE_VERTEXAI=True, GOOGLE_CLOUD_PROJECT, "
            "and GOOGLE_CLOUD_LOCATION=global, then authenticate with "
            "`gcloud auth application-default login`."
        )
        logger.warning("Vertex client unavailable, using deterministic fallback: %s", exc)
        raise runtime_error from exc


def get_session_state(session_id: str) -> SessionState:
    with _session_lock:
        state = _session_store.get(session_id)
        if state is None:
            state = SessionState()
            _session_store[session_id] = state
        return state.model_copy(deep=True)


def remember_session_turn(
    session_id: str,
    req: TurnRequest,
    response: TurnResponse,
    intent: Optional[SearchIntent] = None,
) -> None:
    with _session_lock:
        state = _session_store.get(session_id) or SessionState()
        state.last_user_text = req.user_text
        state.last_assistant_text = response.assistant_text
        state.last_url = req.page_context.url
        state.last_title = req.page_context.title
        state.updated_at = time()

        if intent is not None:
            state.last_intent = intent
            state.last_candidates = response.candidates
        elif response.candidates:
            state.last_candidates = response.candidates

        state.history.append(f"User: {req.user_text}\nAssistant: {response.assistant_text}")
        state.history = state.history[-MAX_SESSION_HISTORY:]
        _session_store[session_id] = state


def extract_search_target(user_text: str) -> str:
    cleaned = user_text.strip()
    cleaned = re.sub(
        r"^(find|show|locate|look for|search for|search|where(?: is| are)?|identify|list|count)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(all|every)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(on|in|at)\s+the\s+page\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .?!")
    return cleaned or user_text.strip()


def make_search_intent_fallback(req: TurnRequest) -> SearchIntent:
    target_query = extract_search_target(req.user_text)
    lower = req.user_text.lower()
    synonyms: List[str] = []

    if "linkedin" in lower:
        synonyms.extend(["share on linkedin", "follow on linkedin", "linkedin icon"])
    if "pricing" in lower:
        synonyms.extend(["plans", "price", "pricing link"])
    if "login" in lower or "sign in" in lower:
        synonyms.extend(["log in", "sign in", "signin"])
    if "download" in lower:
        synonyms.extend(["download", "get started", "install"])

    return sanitize_search_intent(SearchIntent(
        mode="find_occurrences",
        target_query=target_query,
        target_synonyms=synonyms,
        wants_all_occurrences=True,
        wants_navigation=True,
        reason="Heuristic search-intent fallback.",
    ))


def search_terms_from_intent(intent: SearchIntent) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []

    for raw_term in [intent.target_query, *intent.target_synonyms]:
        term = re.sub(r"\s+", " ", (raw_term or "").strip().lower())
        if not term or term in seen:
            continue
        seen.add(term)
        ordered.append(term)

    return ordered


def normalize_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_entity_like_target(target_query: str) -> bool:
    normalized = normalize_phrase(target_query)
    if not normalized:
        return False
    if normalized in GENERIC_TARGET_QUERIES:
        return False
    return len(tokenize(normalized)) <= 3


def sanitize_search_intent(intent: SearchIntent) -> SearchIntent:
    target_query = normalize_phrase(intent.target_query)
    synonyms = [normalize_phrase(synonym) for synonym in intent.target_synonyms if normalize_phrase(synonym)]

    if is_entity_like_target(target_query):
        anchor_tokens = set(tokenize(target_query))
        synonyms = [
            synonym
            for synonym in synonyms
            if anchor_tokens.intersection(tokenize(synonym))
        ]

    deduped_synonyms: List[str] = []
    seen: set[str] = set()
    for synonym in synonyms:
        if not synonym or synonym == target_query or synonym in seen:
            continue
        seen.add(synonym)
        deduped_synonyms.append(synonym)

    return intent.model_copy(
        update={
            "target_query": target_query,
            "target_synonyms": deduped_synonyms[:8],
        }
    )


def candidate_evidence_text(element: Element) -> tuple[str, str]:
    primary = " ".join([
        element.text or "",
        element.aria_label or "",
        element.placeholder or "",
    ]).lower()
    secondary = " ".join([
        element.href or "",
        element.role or "",
        element.tag or "",
        element.input_type or "",
    ]).lower()
    return primary, secondary


def match_strength(term: str, primary: str, secondary: str) -> float:
    normalized = normalize_phrase(term)
    if not normalized:
        return 0.0

    if normalized in primary:
        return 3.0
    if normalized in secondary:
        return 2.6

    tokens = tokenize(normalized)
    if not tokens:
        return 0.0

    primary_hits = sum(1 for token in tokens if token in primary)
    secondary_hits = sum(1 for token in tokens if token in secondary)
    total_hits = primary_hits + secondary_hits

    if primary_hits == len(tokens):
        return 2.3
    if total_hits == len(tokens):
        return 1.9
    if primary_hits > 0:
        return primary_hits * 0.6
    if secondary_hits > 0:
        return secondary_hits * 0.4
    return 0.0


def is_verified_match(intent: SearchIntent, element: Element) -> bool:
    primary, secondary = candidate_evidence_text(element)
    target_strength = match_strength(intent.target_query, primary, secondary)
    synonym_strengths = [match_strength(term, primary, secondary) for term in intent.target_synonyms]

    if is_entity_like_target(intent.target_query):
        return target_strength >= 2.6 or any(score >= 2.6 for score in synonym_strengths)

    if target_strength >= 2.3:
        return True
    return any(score >= 2.3 for score in synonym_strengths)


def verify_candidate_list(intent: SearchIntent, candidates: List[CandidateMatch], elements: List[Element], whole_page: bool) -> List[CandidateMatch]:
    elements_by_id = {element.agent_id: element for element in elements}
    verified: List[CandidateMatch] = []

    for candidate in candidates:
        element = elements_by_id.get(candidate.agent_id)
        if element is None:
            continue
        if not is_verified_match(intent, element):
            continue
        verified.append(candidate)

    return order_candidates(verified, elements, whole_page)[:MAX_GROUNDED_CANDIDATES]


def build_lexical_candidates(intent: SearchIntent, elements: List[Element], limit: int = MAX_GROUNDED_CANDIDATES) -> List[CandidateMatch]:
    terms = search_terms_from_intent(intent)
    if not terms:
        return []

    scored: List[tuple[float, CandidateMatch]] = []
    for element in elements:
        if element.disabled:
            continue

        primary = " ".join([
            element.text or "",
            element.aria_label or "",
            element.placeholder or "",
        ]).lower()
        secondary = " ".join([
            element.href or "",
            element.role or "",
            element.tag or "",
        ]).lower()

        score = 0.0
        reasons: List[str] = []
        for term in terms:
            tokens = tokenize(term)
            if term in primary:
                score += 1.6
                reasons.append(f'Primary text contains "{term}".')
            elif term in secondary:
                score += 1.1
                reasons.append(f'Metadata contains "{term}".')
            else:
                primary_hits = sum(1 for token in tokens if token and token in primary)
                secondary_hits = sum(1 for token in tokens if token and token in secondary)
                if primary_hits:
                    score += primary_hits * 0.55
                if secondary_hits:
                    score += secondary_hits * 0.35

        if score <= 0:
            continue

        if element.tag in {"a", "button"}:
            score += 0.15

        scored.append(
            (
                score,
                CandidateMatch(
                    agent_id=element.agent_id,
                    label=element_label(element),
                    reason=reasons[0] if reasons else "Matched search terms in the DOM.",
                    score=min(1.0, round(score / max(1.6, len(terms) * 1.6), 2)),
                ),
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1].label, item[1].agent_id))
    return [candidate for _, candidate in scored[:limit]]


def merge_candidate_lists(
    primary: List[CandidateMatch],
    secondary: List[CandidateMatch],
    elements: List[Element],
    whole_page: bool,
    limit: int = MAX_GROUNDED_CANDIDATES,
) -> List[CandidateMatch]:
    merged: Dict[str, CandidateMatch] = {
        candidate.agent_id: candidate.model_copy(deep=True) for candidate in primary
    }

    for candidate in secondary:
        existing = merged.get(candidate.agent_id)
        if existing is None:
            merged[candidate.agent_id] = candidate.model_copy(deep=True)
            continue

        existing.score = max(existing.score, candidate.score)
        if not existing.reason and candidate.reason:
            existing.reason = candidate.reason
        if not existing.label and candidate.label:
            existing.label = candidate.label

    ordered = order_candidates(list(merged.values()), elements, whole_page)
    return ordered[:limit]


def format_target_label(intent: SearchIntent) -> str:
    target = (intent.target_query or "").strip()
    if not target:
        return "match"
    return target


def build_search_assistant_text(intent: SearchIntent, candidates: List[CandidateMatch], scan_complete: bool) -> str:
    target_label = format_target_label(intent)
    if candidates:
        noun = "match" if len(candidates) == 1 else "matches"
        return (
            f'I found {len(candidates)} grounded {target_label} {noun} on the page. '
            "Use Next match to move through them."
        )

    if scan_complete:
        return f'I scanned the page and did not find any grounded matches for "{target_label}".'

    return f'I searched the captured part of the page and did not find a grounded match for "{target_label}" yet.'


def ordinal_index_from_text(user_text: str) -> Optional[int]:
    lower = user_text.lower()
    ordinal_map = {
        "first": 0,
        "1st": 0,
        "second": 1,
        "2nd": 1,
        "third": 2,
        "3rd": 2,
        "fourth": 3,
        "4th": 3,
        "fifth": 4,
        "5th": 4,
    }

    for label, index in ordinal_map.items():
        if label in lower:
            return index

    match = re.search(r"\b(\d+)\b", lower)
    if match:
        value = int(match.group(1))
        if value > 0:
            return value - 1

    return None


def ordinal_label(index: int) -> str:
    labels = {
        1: "first",
        2: "second",
        3: "third",
        4: "fourth",
        5: "fifth",
    }
    return labels.get(index, f"{index}th")


def resolve_memory_follow_up(req: TurnRequest, session: SessionState) -> Optional[TurnResponse]:
    if not session.last_candidates or session.last_intent is None:
        return None

    valid_ids = {element.agent_id for element in req.page_context.elements}
    if not valid_ids:
        return None

    lower = req.user_text.lower().strip()
    if not lower:
        return None

    if any(phrase in lower for phrase in ["how many", "count", "number of"]):
        return TurnResponse(
            assistant_text=build_search_assistant_text(session.last_intent, session.last_candidates, True),
            candidates=session.last_candidates,
            commands=[],
            requires_confirmation=False,
        )

    index = ordinal_index_from_text(lower)
    if index is None or index < 0 or index >= len(session.last_candidates):
        return None

    candidate = session.last_candidates[index]
    if candidate.agent_id not in valid_ids:
        return None
    target_label = candidate.label or candidate.agent_id

    if any(keyword in lower for keyword in ["click", "open", "press"]):
        return TurnResponse(
            assistant_text=f'Focusing the {ordinal_label(index + 1)} occurrence: "{target_label}".',
            candidates=session.last_candidates,
            commands=[
                BrowserCommand(kind="scroll", target_agent_id=candidate.agent_id, reason="Focus the requested occurrence."),
                BrowserCommand(kind="highlight", target_agent_id=candidate.agent_id, reason="Highlight the requested occurrence."),
                BrowserCommand(kind="click", target_agent_id=candidate.agent_id, reason="Activate the requested occurrence."),
            ],
            requires_confirmation=True,
        )

    if any(keyword in lower for keyword in ["show", "focus", "highlight", "go to"]):
        return TurnResponse(
            assistant_text=f'Focusing the {ordinal_label(index + 1)} occurrence: "{target_label}".',
            candidates=session.last_candidates,
            commands=[
                BrowserCommand(kind="scroll", target_agent_id=candidate.agent_id, reason="Focus the requested occurrence."),
                BrowserCommand(kind="highlight", target_agent_id=candidate.agent_id, reason="Highlight the requested occurrence."),
            ],
            requires_confirmation=False,
        )

    return TurnResponse(
        assistant_text=f'Focusing the {ordinal_label(index + 1)} occurrence: "{target_label}".',
        candidates=session.last_candidates,
        commands=[
            BrowserCommand(kind="scroll", target_agent_id=candidate.agent_id, reason="Focus the requested occurrence."),
            BrowserCommand(kind="highlight", target_agent_id=candidate.agent_id, reason="Highlight the requested occurrence."),
        ],
        requires_confirmation=False,
    )


def best_matching_element(user_text: str, elements: List[Element]) -> Optional[Element]:
    ranked = rank_matching_elements(user_text, elements, limit=1)
    return ranked[0] if ranked else None


def rank_matching_elements(user_text: str, elements: List[Element], limit: int = 5) -> List[Element]:
    q = set(tokenize(user_text))
    if not q:
        return []

    scored: List[tuple[float, Element]] = []
    for el in elements:
        if el.disabled:
            continue

        primary = " ".join([
            el.text or "",
            el.aria_label or "",
            el.placeholder or "",
        ]).lower()
        secondary = " ".join([
            el.tag or "",
            el.role or "",
            el.href or "",
        ]).lower()

        primary_hits = sum(1 for tok in q if tok in primary)
        secondary_hits = sum(1 for tok in q if tok in secondary)
        score = (primary_hits * 2.0) + secondary_hits

        if score <= 0:
            continue

        if el.tag in {"button", "a"}:
            score += 0.25

        scored.append((score, el))

    scored.sort(key=lambda item: (-item[0], element_label(item[1])))
    return [el for _, el in scored[:limit]]


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
    whole_page_scanned = req.page_context.capture_mode == "whole_page" and req.page_context.scan_complete

    best = best_matching_element(req.user_text, elements)
    ranked = rank_matching_elements(req.user_text, elements, limit=20)
    fallback_candidates = [
        CandidateMatch(
            agent_id=element.agent_id,
            label=element_label(element),
            reason="Matched visible text on the page.",
            score=max(0.1, round(1.0 - (index * 0.12), 2)),
        )
        for index, element in enumerate(ranked)
    ]

    if "type" in user or "fill" in user or "enter" in user:
        text_value = extract_type_text(req.user_text) or "demo text"
        input_el = next(
            (e for e in elements if e.tag in {"input", "textarea"} and not e.disabled),
            None,
        )
        if input_el:
            return TurnResponse(
                assistant_text=f'I found an input field and can type "{text_value}".',
                candidates=[
                    CandidateMatch(
                        agent_id=input_el.agent_id,
                        label=element_label(input_el),
                        reason="Best visible input candidate.",
                        score=1.0,
                    )
                ],
                commands=[
                    BrowserCommand(kind="highlight", target_agent_id=input_el.agent_id),
                    BrowserCommand(kind="type", target_agent_id=input_el.agent_id, text=text_value),
                ],
                requires_confirmation=True,
            )

    if best and any(k in user for k in ["click", "open", "press"]):
        return TurnResponse(
            assistant_text=f'I found a likely target: "{best.text or best.aria_label or best.agent_id}".',
            candidates=fallback_candidates,
            commands=[
                BrowserCommand(kind="highlight", target_agent_id=best.agent_id),
                BrowserCommand(kind="click", target_agent_id=best.agent_id),
            ],
            requires_confirmation=True,
        )

    if best and any(k in user for k in ["find", "show", "go", "scroll", "pricing", "plan"]):
        return TurnResponse(
            assistant_text=(
                f'I found {len(fallback_candidates)} likely match(es) across the scanned page. '
                f'Focusing the best candidate: "{best.text or best.aria_label or best.agent_id}".'
            ),
            candidates=fallback_candidates,
            commands=[
                BrowserCommand(kind="scroll", target_agent_id=best.agent_id),
                BrowserCommand(kind="highlight", target_agent_id=best.agent_id),
            ],
            requires_confirmation=False,
        )

    if any(k in user for k in ["find", "show", "where", "search", "look for", "pricing", "login", "sign in"]) and not best:
        if whole_page_scanned:
            return TurnResponse(
                assistant_text="I scanned the full page and did not find a strong grounded match for that request.",
                candidates=fallback_candidates,
                commands=[],
                requires_confirmation=False,
            )
        return TurnResponse(
            assistant_text="I do not see a strong match in the current viewport, so I will scan further down the page.",
            candidates=fallback_candidates,
            commands=[
                BrowserCommand(
                    kind="scroll_page",
                    direction="down",
                    amount=0.85,
                    reason="Search the next viewport for additional matches.",
                )
            ],
            requires_confirmation=False,
        )

    summary = req.page_context.viewport_text[:3]
    if summary:
        return TurnResponse(
            assistant_text="I could not map that to a precise page action.\n\nCurrent page summary:\n- " + "\n- ".join(summary),
            candidates=fallback_candidates,
            commands=[],
            requires_confirmation=False,
        )

    return TurnResponse(
        assistant_text="I could not infer a safe page action.",
        candidates=fallback_candidates,
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
        direction = (cmd.get("direction") or "").strip().lower() or None
        amount_raw = cmd.get("amount")

        if kind not in ALLOWED_KINDS:
            continue

        if kind in {"highlight", "scroll", "click", "type"}:
            if not target_agent_id or target_agent_id not in valid_ids:
                continue

        if kind == "type" and not isinstance(text, str):
            continue

        amount = None
        if kind == "scroll_page":
            if direction not in {"up", "down"}:
                continue
            try:
                amount = float(amount_raw) if amount_raw is not None else 0.85
            except (TypeError, ValueError):
                continue
            amount = max(0.2, min(1.5, amount))

        out.append(
            BrowserCommand(
                kind=kind,
                target_agent_id=target_agent_id,
                text=text,
                reason=reason,
                direction=direction,
                amount=amount,
            )
        )
    return out


def validate_candidates(candidates: List[Dict[str, Any]], elements: List[Element]) -> List[CandidateMatch]:
    elements_by_id = {element.agent_id: element for element in elements}
    out: List[CandidateMatch] = []
    seen: set[str] = set()

    for candidate in candidates:
        agent_id = (candidate.get("agent_id") or "").strip()
        if not agent_id or agent_id in seen or agent_id not in elements_by_id:
            continue

        score_raw = candidate.get("score", 0.0)
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0

        element = elements_by_id[agent_id]
        out.append(
            CandidateMatch(
                agent_id=agent_id,
                label=(candidate.get("label") or element_label(element)).strip(),
                reason=(candidate.get("reason") or "").strip(),
                score=max(0.0, min(1.0, score)),
            )
        )
        seen.add(agent_id)

    return out


def order_candidates(candidates: List[CandidateMatch], elements: List[Element], whole_page: bool) -> List[CandidateMatch]:
    elements_by_id = {element.agent_id: element for element in elements}

    def candidate_position(agent_id: str) -> tuple[float, float]:
        element = elements_by_id.get(agent_id)
        if element is None:
            return (float("inf"), float("inf"))
        y, x, _ = page_sort_key(element)
        return (y, x)

    if whole_page:
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate_position(candidate.agent_id)[0],
                candidate_position(candidate.agent_id)[1],
                -candidate.score,
                candidate.agent_id,
            ),
        )

    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.label, candidate.agent_id))


def choose_model(user_text: str, has_image: bool = False) -> str:
    lower = user_text.lower()
    if has_image:
        return VISION_MODEL
    if any(k in lower for k in ["compare", "plan", "workflow", "step by step", "deeply", "analyze"]):
        return GENERALIST_MODEL
    return DEFAULT_MODEL


def is_search_request(user_text: str) -> bool:
    lower = user_text.lower()
    return any(keyword in lower for keyword in [
        "find",
        "show",
        "where",
        "search",
        "look for",
        "pricing",
        "login",
        "sign in",
        "sign-in",
        "download",
    ])


def call_generalist_intent(req: TurnRequest, session: SessionState) -> SearchIntent:
    client = get_client()
    fallback = make_search_intent_fallback(req)
    memory_lines = session.history[-3:] if session.history else []

    prompt = f"""
You are the generalist planner for a browser agent.

Return STRICT JSON only.
No markdown.
No explanation outside JSON.

Schema:
{{
  "mode": "find_occurrences|click_target|type_into_target|summarize_page|answer_question",
  "target_query": "string",
  "target_synonyms": ["string"],
  "wants_all_occurrences": true_or_false,
  "wants_navigation": true_or_false,
  "reason": "short reason"
}}

Rules:
- If the user wants something located on the page, use mode="find_occurrences".
- Normalize target_query to the concrete thing to look for on the page.
- target_synonyms should include likely aliases that might appear in link text, aria labels, or href values.
- For brand/entity searches, every synonym must keep the unique brand/entity token. Do not add broad generic terms like "share icon" or "social icon".
- If the user asks to find/show/locate/list/count/all/every occurrence, set wants_all_occurrences=true.
- If the user wants to inspect or jump between results, set wants_navigation=true.
- Prefer concise target_query values such as "linkedin", "pricing", "sign in", or "download".

Previous search context:
{json.dumps({
    "last_intent": session.last_intent.model_dump() if session.last_intent else None,
    "last_candidates": [candidate.model_dump() for candidate in session.last_candidates[:5]],
    "history": memory_lines,
}, ensure_ascii=False)}

Current request:
{req.user_text}

Page:
URL: {req.page_context.url}
Title: {req.page_context.title}
Text summary:
{chr(10).join(req.page_context.viewport_text[:20])}
""".strip()

    response = client.models.generate_content(
        model=GENERALIST_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )

    raw = strip_fences(response.text or "")
    data = json.loads(raw)
    mode = (data.get("mode") or "find_occurrences").strip().lower()
    target_query = (data.get("target_query") or fallback.target_query).strip()
    target_synonyms = [
        synonym.strip()
        for synonym in data.get("target_synonyms", [])
        if isinstance(synonym, str) and synonym.strip()
    ]

    if mode not in {"find_occurrences", "click_target", "type_into_target", "summarize_page", "answer_question"}:
        mode = fallback.mode

    if not target_query:
        target_query = fallback.target_query

    if not target_synonyms:
        target_synonyms = fallback.target_synonyms

    return sanitize_search_intent(SearchIntent(
        mode=mode,
        target_query=target_query,
        target_synonyms=target_synonyms[:8],
        wants_all_occurrences=bool(data.get("wants_all_occurrences", True)),
        wants_navigation=bool(data.get("wants_navigation", True)),
        reason=(data.get("reason") or fallback.reason).strip(),
    ))


def call_specialist_locator(req: TurnRequest, intent: SearchIntent) -> List[CandidateMatch]:
    client = get_client()
    whole_page_scan = req.page_context.capture_mode == "whole_page"
    sorted_elements = sorted(req.page_context.elements, key=page_sort_key)

    prompt = f"""
You are the precise locator specialist for a browser agent.

Return STRICT JSON only.
No markdown.
No explanation outside JSON.

Schema:
{{
  "candidates": [
    {{
      "agent_id": "existing agent_id",
      "label": "short label",
      "reason": "why this grounded element matches",
      "score": 0.0
    }}
  ],
  "notes": "short note"
}}

Rules:
- Inspect every supplied screenshot and metadata block before answering.
- Return EVERY grounded occurrence that matches the target, up to {MAX_GROUNDED_CANDIDATES}.
- Distinct page locations are distinct occurrences even if the label is the same.
- Use ONLY the provided agent_ids.
- Prefer high recall, but do not invent matches.
- Reject near-misses that do not explicitly match the target entity or alias.
- Use aria labels, href values, and surrounding text cues for icon-only links.
- Do not return commands.

Search brief:
{json.dumps(intent.model_dump(), ensure_ascii=False)}

Page:
URL: {req.page_context.url}
Title: {req.page_context.title}
Capture mode: {req.page_context.capture_mode}
Whole-page scan complete: {json.dumps(req.page_context.scan_complete)}
Element count: {len(sorted_elements)}
Elements:
{json.dumps([element.model_dump() for element in sorted_elements[:220]], ensure_ascii=False)}
""".strip()

    parts = build_multimodal_parts(req, prompt)
    has_image = any(getattr(part, "inline_data", None) is not None for part in parts)

    response = client.models.generate_content(
        model=SPECIALIST_MODEL if has_image else DEFAULT_MODEL,
        contents=types.UserContent(parts=parts),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )

    raw = strip_fences(response.text or "")
    data = json.loads(raw)
    model_candidates = validate_candidates(data.get("candidates", []), req.page_context.elements)
    lexical_candidates = build_lexical_candidates(intent, req.page_context.elements)
    merged = merge_candidate_lists(
        model_candidates,
        lexical_candidates,
        req.page_context.elements,
        whole_page=whole_page_scan,
        limit=MAX_GROUNDED_CANDIDATES,
    )
    return verify_candidate_list(intent, merged, req.page_context.elements, whole_page_scan)


def run_search_orchestrator(req: TurnRequest, session: SessionState) -> tuple[TurnResponse, SearchIntent]:
    try:
        intent = call_generalist_intent(req, session)
    except Exception as exc:
        logger.warning("Generalist intent step failed, using heuristic search intent: %s", exc)
        intent = sanitize_search_intent(make_search_intent_fallback(req))

    try:
        candidates = call_specialist_locator(req, intent)
    except Exception as exc:
        logger.warning("Specialist locator step failed, using lexical fallback: %s", exc)
        candidates = build_lexical_candidates(intent, req.page_context.elements)

    candidates = verify_candidate_list(
        intent,
        candidates,
        req.page_context.elements,
        req.page_context.capture_mode == "whole_page",
    )

    assistant_text = build_search_assistant_text(intent, candidates, req.page_context.scan_complete)
    commands: List[BrowserCommand] = []
    requires_confirmation = False

    if candidates:
        commands = [
            BrowserCommand(kind="scroll", target_agent_id=candidates[0].agent_id, reason="Focus the first grounded match."),
            BrowserCommand(kind="highlight", target_agent_id=candidates[0].agent_id, reason="Highlight the first grounded match."),
        ]

    return (
        TurnResponse(
            assistant_text=assistant_text,
            candidates=candidates,
            commands=commands,
            requires_confirmation=requires_confirmation,
        ),
        intent,
    )


def call_vertex(req: TurnRequest) -> TurnResponse:
    valid_ids = {e.agent_id for e in req.page_context.elements}
    client = get_client()
    is_pdf_page = is_pdf_url(req.page_context.url)
    whole_page_scan = req.page_context.capture_mode == "whole_page"
    capture_count = len(req.page_context.viewport_captures)
    viewport = req.page_context.viewport.model_dump() if req.page_context.viewport else None
    sorted_elements = sorted(req.page_context.elements, key=page_sort_key)
    aggregated_elements = [element.model_dump() for element in sorted_elements[:180]]

    prompt = f"""
You are the browser-page specialist for a Chrome extension.

Return STRICT JSON only.
No markdown.
No explanation outside JSON.

Schema:
{{
  "assistant_text": "string",
  "requires_confirmation": true_or_false,
  "candidates": [
    {{
      "agent_id": "existing agent_id",
      "label": "short label",
      "reason": "why this candidate matches",
      "score": 0.0
    }}
  ],
  "commands": [
    {{
      "kind": "highlight|scroll|scroll_page|click|type",
      "target_agent_id": "existing agent_id or null",
      "text": "string or null",
      "reason": "short reason",
      "direction": "up|down|null",
      "amount": 0.0
    }}
  ]
}}

Rules:
- Use ONLY the provided agent_ids.
- Use the screenshots, viewport metrics, and element bounding boxes for visual grounding when screenshots are attached.
- Prefer the smallest safe action.
- For requests like "find/show/go/pricing", return every grounded occurrence you can justify, up to 20 ranked candidates.
- If a full-page scan is attached, inspect ALL viewport captures before deciding and do not ask for scroll_page. Return all grounded matches across the scanned page.
- If a full-page scan is not attached and the requested target is not visible yet, return a single scroll_page command with direction="down".
- For click/type, set requires_confirmation=true.
- If multiple plausible matches exist, keep all of them in candidates so the client can jump from one bounding box to the next.
- If the page is a PDF and text is available, answer the user's question from that text and return no commands.
- If unsure, return no commands and explain briefly.

User request:
{req.user_text}

Page:
Page kind: {"pdf_document" if is_pdf_page else "web_page"}
URL: {req.page_context.url}
Title: {req.page_context.title}
Selected text: {req.page_context.selected_text}
Capture mode: {req.page_context.capture_mode}
Full-page scan complete: {json.dumps(req.page_context.scan_complete)}
Viewport capture count: {capture_count}
Viewport: {json.dumps(viewport, ensure_ascii=False)}

Whole-page text summary:
{chr(10).join(req.page_context.viewport_text[:15])}

Aggregated elements:
{json.dumps(aggregated_elements, ensure_ascii=False)}
""".strip()

    parts = build_multimodal_parts(req, prompt)
    has_image = any(getattr(part, "inline_data", None) is not None for part in parts)

    response = client.models.generate_content(
        model=choose_model(req.user_text, has_image=has_image),
        contents=types.UserContent(parts=parts),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw = strip_fences(response.text or "")
    data = json.loads(raw)

    commands = validate_commands(data.get("commands", []), valid_ids)
    candidates = validate_candidates(data.get("candidates", []), req.page_context.elements)

    if not candidates:
        command_candidate_ids = []
        seen_ids: set[str] = set()
        for command in commands:
            if command.target_agent_id and command.target_agent_id not in seen_ids:
                seen_ids.add(command.target_agent_id)
                command_candidate_ids.append(command.target_agent_id)

        elements_by_id = {element.agent_id: element for element in req.page_context.elements}
        candidates = [
            CandidateMatch(
                agent_id=agent_id,
                label=element_label(elements_by_id[agent_id]),
                reason="Derived from the primary command target.",
                score=max(0.1, round(1.0 - (index * 0.15), 2)),
            )
            for index, agent_id in enumerate(command_candidate_ids)
            if agent_id in elements_by_id
        ]

    candidates = order_candidates(candidates, req.page_context.elements, whole_page_scan)

    if not commands and not candidates and is_search_request(req.user_text) and not is_pdf_page and not whole_page_scan:
        assistant_text = "I do not see the requested target in the current viewport, so I will continue scanning further down the page."
        commands = [
            BrowserCommand(
                kind="scroll_page",
                direction="down",
                amount=0.85,
                reason="Search the next viewport for the requested target.",
            )
        ]
    else:
        assistant_text = data.get("assistant_text", "Done.")

    if whole_page_scan and not candidates and is_search_request(req.user_text):
        if req.page_context.scan_complete:
            assistant_text = data.get(
                "assistant_text",
                "I scanned the full page and did not find a grounded match for that request.",
            )
        else:
            assistant_text = data.get(
                "assistant_text",
                "I scanned the captured portion of the page and did not find a grounded match yet.",
            )
        commands = []

    if whole_page_scan and candidates:
        commands = [command for command in commands if command.kind != "scroll_page"]

    if candidates and not commands and is_search_request(req.user_text):
        commands = [
            BrowserCommand(kind="scroll", target_agent_id=candidates[0].agent_id, reason="Focus the first grounded match."),
            BrowserCommand(kind="highlight", target_agent_id=candidates[0].agent_id, reason="Highlight the first grounded match."),
        ]

    requires_confirmation = bool(data.get("requires_confirmation", False))

    return TurnResponse(
        assistant_text=assistant_text,
        candidates=candidates,
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
    req = req.model_copy(
        update={
            "page_context": enrich_page_context(req.page_context)
        }
    )

    session = get_session_state(req.session_id)
    memory_response = resolve_memory_follow_up(req, session)
    if memory_response is not None:
        remember_session_turn(req.session_id, req, memory_response, session.last_intent)
        return memory_response

    if is_search_request(req.user_text):
        try:
            response, intent = run_search_orchestrator(req, session)
            remember_session_turn(req.session_id, req, response, intent)
            return response
        except Exception as e:
            logger.warning("Search orchestrator failed, using fallback: %s", e)
            response = deterministic_fallback(req)
            remember_session_turn(req.session_id, req, response, make_search_intent_fallback(req))
            return response

    try:
        response = call_vertex(req)
        remember_session_turn(req.session_id, req, response, None)
        return response
    except Exception as e:
        logger.warning("Vertex call failed, using fallback: %s", e)
        response = deterministic_fallback(req)
        remember_session_turn(req.session_id, req, response, None)
        return response
