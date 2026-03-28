const DEFAULT_BACKEND = "http://127.0.0.1:8000";

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
      const pageContext = await requestTabContext(tab);

      let screenshot = null;
      try {
        screenshot = await chrome.tabs.captureVisibleTab(tab.windowId, {
          format: "jpeg",
          quality: 60
        });
      } catch (e) {
        console.warn("captureVisibleTab failed:", e);
      }

      sendResponse({
        ok: true,
        pageContext: {
          ...pageContext,
          screenshot_data_url: screenshot
        }
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
