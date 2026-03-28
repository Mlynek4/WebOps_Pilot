const DEFAULT_BACKEND = "http://127.0.0.1:8000";
const WHOLE_PAGE_SCROLL_RATIO = 0.85;
const WHOLE_PAGE_SETTLE_MS = 350;
const MAX_WHOLE_PAGE_CAPTURES = 20;

function initSidePanelBehavior() {
  return chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

async function getBackendBase() {
  try {
    const stored = await chrome.storage.local.get("backend_url");
    return stored.backend_url || DEFAULT_BACKEND;
  } catch (e) {
    console.warn("Failed to read backend_url, using default", e);
    return DEFAULT_BACKEND;
  }
}

function getUnsupportedPageError(error) {
  return new Error(
    "This tab is not available to the extension yet. Open a normal http/https page, wait for it to finish loading, and try again. " +
      `Details: ${error instanceof Error ? error.message : String(error)}`
  );
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function isSupportedTab(tab) {
  const url = tab.url || "";
  return url.startsWith("http://") || url.startsWith("https://");
}

function isMissingReceiverError(error) {
  return String(error).includes("Receiving end does not exist");
}

chrome.runtime.onInstalled.addListener(() => {
  initSidePanelBehavior().catch(console.error);
});

chrome.runtime.onStartup.addListener(() => {
  initSidePanelBehavior().catch(console.error);
});

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) throw new Error("No active tab");
  return tabs[0];
}

function getTabId(tab) {
  if (typeof tab.id !== "number") {
    throw new Error("Active tab has no id");
  }
  return tab.id;
}

async function sendTabMessage(tab, payload) {
  return chrome.tabs.sendMessage(getTabId(tab), payload);
}

async function ensureContentScript(tab) {
  if (!isSupportedTab(tab)) {
    throw getUnsupportedPageError(
      new Error(`Unsupported URL: ${tab.url || "unknown"}`)
    );
  }

  try {
    await sendTabMessage(tab, { type: "PING" });
    return;
  } catch (error) {
    if (!isMissingReceiverError(error)) {
      throw getUnsupportedPageError(error);
    }
  }

  try {
    await chrome.scripting.executeScript({
      target: { tabId: getTabId(tab) },
      files: ["content.js"]
    });
  } catch (error) {
    throw getUnsupportedPageError(error);
  }
}

async function requestTabContext(tab) {
  try {
    await ensureContentScript(tab);
    return await sendTabMessage(tab, { type: "EXTRACT_CONTEXT" });
  } catch (error) {
    throw getUnsupportedPageError(error);
  }
}

async function clearTabOverlay(tab) {
  try {
    await ensureContentScript(tab);
    await sendTabMessage(tab, { type: "CLEAR_OVERLAY" });
  } catch (error) {
    console.warn("Failed to clear overlay before capture", error);
  }
}

async function scrollTabToY(tab, scrollY) {
  await ensureContentScript(tab);
  return sendTabMessage(tab, { type: "SCROLL_TO_Y", scroll_y: scrollY });
}

async function captureViewportScreenshot(tab) {
  try {
    return await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "png"
    });
  } catch (error) {
    console.warn("captureVisibleTab failed:", error);
    return null;
  }
}

function mergeElements(existing, incoming) {
  const merged = new Map(existing.map((element) => [element.agent_id, element]));

  for (const element of incoming || []) {
    if (!element?.agent_id) continue;
    const current = merged.get(element.agent_id);
    if (!current) {
      merged.set(element.agent_id, element);
      continue;
    }

    const currentLabel = `${current.text || ""}${current.aria_label || ""}${current.placeholder || ""}`;
    const incomingLabel = `${element.text || ""}${element.aria_label || ""}${element.placeholder || ""}`;
    if (incomingLabel.length > currentLabel.length) {
      merged.set(element.agent_id, { ...current, ...element });
    }
  }

  return Array.from(merged.values());
}

function mergeViewportText(captures) {
  const lines = [];
  const seen = new Set();

  for (const capture of captures) {
    for (const line of capture.viewport_text || []) {
      const normalized = line.trim();
      if (!normalized || seen.has(normalized)) continue;
      seen.add(normalized);
      lines.push(normalized);
    }
  }

  return lines.slice(0, 200);
}

async function captureWholePageContext(tab) {
  await ensureContentScript(tab);
  await clearTabOverlay(tab);

  const initialContext = await requestTabContext(tab);
  const originalScrollY = initialContext.viewport?.scroll_y || 0;
  const viewportHeight = Math.max(1, Math.round(initialContext.viewport?.height || 1));
  const scrollStep = Math.max(200, Math.round(viewportHeight * WHOLE_PAGE_SCROLL_RATIO));
  const captures = [];
  const seenScrollPositions = new Set();
  let scanComplete = true;
  let lastContext = initialContext;

  try {
    let targetScrollY = 0;

    for (let index = 0; index < MAX_WHOLE_PAGE_CAPTURES; index += 1) {
      await scrollTabToY(tab, targetScrollY);
      await sleep(WHOLE_PAGE_SETTLE_MS);

      const context = await requestTabContext(tab);
      const actualScrollY = Math.max(0, Math.round(context.viewport?.scroll_y || 0));

      if (seenScrollPositions.has(actualScrollY)) {
        lastContext = context;
        break;
      }

      seenScrollPositions.add(actualScrollY);

      captures.push({
        index,
        scroll_y: actualScrollY,
        viewport: context.viewport,
        viewport_text: context.viewport_text,
        elements: context.elements,
        screenshot_data_url: await captureViewportScreenshot(tab)
      });

      lastContext = context;

      const maxScrollY = Math.max(
        0,
        Math.round(
          context.viewport?.max_scroll_y ??
            Math.max(0, (context.viewport?.page_height || viewportHeight) - viewportHeight)
        )
      );

      if (actualScrollY >= maxScrollY) {
        break;
      }

      const nextScrollY = Math.min(actualScrollY + scrollStep, maxScrollY);
      if (nextScrollY <= actualScrollY) {
        break;
      }

      targetScrollY = nextScrollY;
    }
  } finally {
    await scrollTabToY(tab, originalScrollY).catch((error) => {
      console.warn("Failed to restore original scroll position", error);
    });
    await sleep(150);
  }

  if (captures.length) {
    const finalScrollY = captures[captures.length - 1].scroll_y || 0;
    const finalMaxScrollY = Math.max(
      0,
      Math.round(
        lastContext.viewport?.max_scroll_y ??
          Math.max(0, (lastContext.viewport?.page_height || viewportHeight) - viewportHeight)
      )
    );
    scanComplete = finalScrollY >= finalMaxScrollY;
  }

  const pageContext = {
    ...initialContext,
    viewport_text: mergeViewportText(captures.length ? captures : [initialContext]),
    elements: mergeElements([], captures.flatMap((capture) => capture.elements || [])),
    viewport_captures: captures,
    capture_mode: captures.length ? "whole_page" : "viewport",
    scan_complete: scanComplete,
    screenshot_data_url: captures[0]?.screenshot_data_url || null,
    viewport: {
      ...initialContext.viewport,
      page_width: Math.max(...captures.map((capture) => capture.viewport?.page_width || 0), initialContext.viewport?.page_width || 0),
      page_height: Math.max(...captures.map((capture) => capture.viewport?.page_height || 0), initialContext.viewport?.page_height || 0),
      max_scroll_y: Math.max(...captures.map((capture) => capture.viewport?.max_scroll_y || 0), initialContext.viewport?.max_scroll_y || 0),
      scroll_y: originalScrollY
    }
  };

  if (!captures.length) {
    return {
      ...initialContext,
      screenshot_data_url: await captureViewportScreenshot(tab),
      viewport_captures: [],
      capture_mode: "viewport",
      scan_complete: true
    };
  }

  return pageContext;
}

async function executeTabCommands(tab, commands) {
  try {
    await ensureContentScript(tab);
    return await sendTabMessage(tab, {
      type: "EXECUTE_COMMANDS",
      commands
    });
  } catch (error) {
    throw getUnsupportedPageError(error);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "GET_CONTEXT") {
      const tab = await getActiveTab();
      const pageContext = await captureWholePageContext(tab);

      sendResponse({
        ok: true,
        pageContext
      });
      return;
    }

    if (message.type === "RUN_TURN") {
      const backend = await getBackendBase();

      const res = await fetch(`${backend}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message.payload)
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Backend error ${res.status}: ${text}`);
      }

      const data = await res.json();
      sendResponse({ ok: true, data });
      return;
    }

    if (message.type === "EXECUTE_COMMANDS") {
      const tab = await getActiveTab();
      const result = await executeTabCommands(tab, message.commands);
      sendResponse({ ok: true, result });
      return;
    }

    sendResponse({ ok: false, error: "Unknown message type" });
  })().catch((error) => {
    sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
  });

  return true;
});
