# WebOps Pilot

**WebOps Pilot** is a multimodal browser agent that turns any webpage into an interactive, executable interface. Instead of manually navigating, users can issue natural language commands, and the agent understands the page, plans actions, and executes them directly in the browser.

---

## What it does

WebOps Pilot observes the current webpage (DOM structure, visible text, and screenshot) and uses a tri-agent architecture to:

- understand user intent  
- locate relevant UI elements  
- generate action plans (scroll, click, type, highlight)  
- execute them deterministically in the browser  

This transforms passive browsing into an **agent-driven experience**.

---

## How it works

The system is built around a **Default + Generalist + Vision agent architecture**:

1. **Context Extraction (Chrome Extension)**
   - Collects structured DOM elements (buttons, links, inputs)
   - Captures visible text and screenshot
   - Assigns stable `agent_id` to interactive elements

2. **Reasoning (Generalist Agent)**
   - Processes user intent
   - Uses multimodal reasoning (text + screenshot)
   - Decomposes tasks into actionable steps

3. **Grounding & Execution (Specialist Agent)**
   - Maps actions to real DOM elements
   - Generates structured commands:
     - `click`
     - `scroll`
     - `type`
     - `highlight`

4. **Execution Layer (Content Script)**
   - Executes commands in the browser
   - Provides visual overlay feedback

---

## Tech Stack

- Google ADK (agent orchestration)
- Vertex AI (inference platform)
- Gemini 3.1 Flash Live (multimodal reasoning)
- FastAPI (backend API)
- Chrome Extensions (Manifest V3)
- Python (backend logic)

---

## Why it’s different

Unlike traditional copilots or RPA tools:

- **No predefined selectors** — works on arbitrary websites  
- **Multimodal grounding** — combines DOM + vision  
- **Agent-based execution** — plans and acts, not just answers  
- **In-browser control loop** — real actions, not suggestions  

---

## Example Use Cases

- Navigate complex dashboards  
- Find and click relevant information  
- Autofill forms  
- Guide users step-by-step on unfamiliar websites  
- Accessibility via voice-driven browsing  

---

## Project Status

Prototype with:
- working Chrome extension  
- backend agent loop  
- real-time command execution on webpages  

---

## Vision

WebOps Pilot enables a new paradigm:  
**websites become programmable environments controlled by intelligent agents.**
