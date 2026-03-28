const BACKEND = "http://localhost:8000";

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch(console.error);
});

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) throw new Error("No active tab");
  return tabs[0];
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "GET_CONTEXT") {
      const tab = await getActiveTab();

      const pageContext = await chrome.tabs.sendMessage(tab.id, { type: "EXTRACT_CONTEXT" });

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
      const res = await fetch(`${BACKEND}/turn`, {
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
      const result = await chrome.tabs.sendMessage(tab.id, {
        type: "EXECUTE_COMMANDS",
        commands: message.commands
      });
      sendResponse({ ok: true, result });
      return;
    }

    sendResponse({ ok: false, error: "Unknown message type" });
  })().catch((error) => {
    sendResponse({ ok: false, error: String(error) });
  });

  return true;
});