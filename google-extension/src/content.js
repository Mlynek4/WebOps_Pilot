if (!globalThis.__pageAgentContentScriptLoaded) {
  globalThis.__pageAgentContentScriptLoaded = true;

  const OVERLAY_ID = "__page_agent_overlay__";
  let domVersion = 0;
  let nextId = 1;

  function ensureOverlayRoot() {
    let root = document.getElementById(OVERLAY_ID);
    if (!root) {
      root = document.createElement("div");
      root.id = OVERLAY_ID;
      root.style.position = "fixed";
      root.style.left = "0";
      root.style.top = "0";
      root.style.width = "100vw";
      root.style.height = "100vh";
      root.style.pointerEvents = "none";
      root.style.zIndex = "2147483647";
      document.documentElement.appendChild(root);
    }
    return root;
  }

  function clearOverlay() {
    const root = ensureOverlayRoot();
    root.innerHTML = "";
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return (
      style.visibility !== "hidden" &&
      style.display !== "none" &&
      rect.width > 4 &&
      rect.height > 4 &&
      rect.bottom >= 0 &&
      rect.right >= 0 &&
      rect.top <= window.innerHeight &&
      rect.left <= window.innerWidth
    );
  }

  function getOrSetAgentId(el) {
    if (!el.dataset.agentId) {
      el.dataset.agentId = `e_${String(nextId++).padStart(4, "0")}`;
    }
    return el.dataset.agentId;
  }

  function collectElements() {
    const candidates = Array.from(
      document.querySelectorAll(
        "button, a[href], input, textarea, select, [role='button'], [role='textbox'], [contenteditable='true']"
      )
    );

    return candidates
      .filter(isVisible)
      .slice(0, 120)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          agent_id: getOrSetAgentId(el),
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute("role") || "",
          text: (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200),
          aria_label: el.getAttribute("aria-label") || "",
          placeholder: el.getAttribute("placeholder") || "",
          href: el.getAttribute("href"),
          input_type: el.getAttribute("type"),
          is_content_editable: el.isContentEditable,
          disabled: !!el.disabled,
          bbox: {
            x: rect.left,
            y: rect.top,
            w: rect.width,
            h: rect.height
          }
        };
      });
  }

  function getViewportText() {
    const text = (document.body?.innerText || "")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, 6000);
    return text ? text.split("\n").map(s => s.trim()).filter(Boolean).slice(0, 30) : [];
  }

  function extractContext() {
    domVersion += 1;
    return {
      url: location.href,
      title: document.title,
      selected_text: String(window.getSelection() || ""),
      viewport_text: getViewportText(),
      elements: collectElements(),
      dom_version: domVersion
    };
  }

  function getNodeByAgentId(agentId) {
    return document.querySelector(`[data-agent-id="${CSS.escape(agentId)}"]`);
  }

  function highlight(agentId) {
    const el = getNodeByAgentId(agentId);
    if (!el) return { ok: false, error: `Target not found: ${agentId}` };

    const rect = el.getBoundingClientRect();
    const root = ensureOverlayRoot();

    const box = document.createElement("div");
    box.style.position = "fixed";
    box.style.left = `${rect.left}px`;
    box.style.top = `${rect.top}px`;
    box.style.width = `${rect.width}px`;
    box.style.height = `${rect.height}px`;
    box.style.border = "3px solid #ff2d55";
    box.style.borderRadius = "8px";
    box.style.boxSizing = "border-box";
    box.style.background = "rgba(255,45,85,0.08)";
    root.appendChild(box);

    return { ok: true };
  }

  function scrollToElement(agentId) {
    const el = getNodeByAgentId(agentId);
    if (!el) return { ok: false, error: `Target not found: ${agentId}` };
    el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
    return { ok: true };
  }

  function clickElement(agentId) {
    const el = getNodeByAgentId(agentId);
    if (!el) return { ok: false, error: `Target not found: ${agentId}` };
    if ("disabled" in el && el.disabled) {
      return { ok: false, error: `Target is disabled: ${agentId}` };
    }
    el.click();
    return { ok: true };
  }

  function setFormValue(el, text) {
    const prototype = Object.getPrototypeOf(el);
    const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, "value");

    if (descriptor && typeof descriptor.set === "function") {
      descriptor.set.call(el, text);
      return;
    }

    el.value = text;
  }

  function typeInto(agentId, text) {
    const el = getNodeByAgentId(agentId);
    if (!el) return { ok: false, error: `Target not found: ${agentId}` };

    el.focus();

    if (el.isContentEditable) {
      el.textContent = text;
    } else if ("value" in el) {
      setFormValue(el, text);
    } else {
      return { ok: false, error: `Target does not accept text: ${agentId}` };
    }

    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));

    return { ok: true };
  }

  function executeCommands(commands) {
    clearOverlay();
    const results = [];

    for (const cmd of commands) {
      try {
        let result = { ok: false, error: "Unknown command" };

        if (cmd.kind === "highlight") result = highlight(cmd.target_agent_id);
        if (cmd.kind === "scroll") result = scrollToElement(cmd.target_agent_id);
        if (cmd.kind === "click") result = clickElement(cmd.target_agent_id);
        if (cmd.kind === "type") result = typeInto(cmd.target_agent_id, cmd.text || "");

        results.push({ command: cmd, ...result });

        if (!result.ok) break;
      } catch (e) {
        results.push({ command: cmd, ok: false, error: String(e) });
        break;
      }
    }

    return { results };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "PING") {
      sendResponse({ ok: true });
      return true;
    }

    if (message.type === "EXTRACT_CONTEXT") {
      sendResponse(extractContext());
      return true;
    }

    if (message.type === "EXECUTE_COMMANDS") {
      sendResponse(executeCommands(message.commands || []));
      return true;
    }
  });
}
