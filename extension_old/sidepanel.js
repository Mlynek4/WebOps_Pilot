const output = document.getElementById("output");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("sendBtn");
const runBtn = document.getElementById("runBtn");

let pendingCommands = [];
const sessionId = crypto.randomUUID();

function setOutput(text) {
  output.textContent = text;
}

async function getContext() {
  const res = await chrome.runtime.sendMessage({ type: "GET_CONTEXT" });
  if (!res?.ok) throw new Error(/*res?.error*/'gówno' || "GET_CONTEXT failed");
  return res.pageContext;
}

async function runTurn(userText, pageContext) {
  const payload = {
    session_id: sessionId,
    user_text: userText,
    page_context: pageContext
  };

  const res = await chrome.runtime.sendMessage({
    type: "RUN_TURN",
    payload
  });

  if (!res?.ok) throw new Error(res?.error || "RUN_TURN failed");
  return res.data;
}

async function executeCommands(commands) {
  const res = await chrome.runtime.sendMessage({
    type: "EXECUTE_COMMANDS",
    commands
  });

  if (!res?.ok) throw new Error(res?.error || "EXECUTE_COMMANDS failed");
  return res.result;
}

sendBtn.addEventListener("click", async () => {
  try {
    const userText = promptEl.value.trim();
    if (!userText) return;

    setOutput("Collecting page context...");
    runBtn.style.display = "none";
    pendingCommands = [];

    const pageContext = await getContext();
    const result = await runTurn(userText, pageContext);

    const lines = [
      result.assistant_text || "(no assistant text)",
      "",
      "Commands:",
      JSON.stringify(result.commands || [], null, 2),
      "",
      `requires_confirmation: ${Boolean(result.requires_confirmation)}`
    ];
    setOutput(lines.join("\n"));

    const commands = result.commands || [];
    if (!commands.length) return;

    const autoSafe = commands.every(c => c.kind === "highlight" || c.kind === "scroll");

    if (autoSafe && !result.requires_confirmation) {
      const execResult = await executeCommands(commands);
      setOutput(lines.join("\n") + "\n\nExecuted:\n" + JSON.stringify(execResult, null, 2));
      return;
    }

    pendingCommands = commands;
    runBtn.style.display = "inline-block";
  } catch (e) {
    setOutput(String(e));
  }
});

runBtn.addEventListener("click", async () => {
  try {
    if (!pendingCommands.length) return;

    const execResult = await executeCommands(pendingCommands);
    setOutput(output.textContent + "\n\nExecuted:\n" + JSON.stringify(execResult, null, 2));
    pendingCommands = [];
    runBtn.style.display = "none";
  } catch (e) {
    setOutput(String(e));
  }
});