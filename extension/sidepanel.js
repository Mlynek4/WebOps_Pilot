const output = document.getElementById("output");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("sendBtn");
const runBtn = document.getElementById("runBtn");
const nextBtn = document.getElementById("nextBtn");

let pendingCommands = [];
let candidateMatches = [];
let activeCandidateIndex = -1;
let baseOutputText = "";
const sessionId = crypto.randomUUID();
const MAX_AUTO_SEARCH_STEPS = 8;

function setOutput(text) {
  output.textContent = text;
}

function getErrorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function resetMatches() {
  candidateMatches = [];
  activeCandidateIndex = -1;
  baseOutputText = "";
  nextBtn.style.display = "none";
  nextBtn.disabled = false;
}

function formatScore(score) {
  return typeof score === "number" && Number.isFinite(score) ? score.toFixed(2) : "0.00";
}

function candidateLabel(candidate) {
  return candidate.label || candidate.agent_id;
}

function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function mergeCandidates(existing, incoming) {
  const merged = [...existing];
  const seen = new Set(existing.map((candidate) => candidate.agent_id));

  for (const candidate of incoming || []) {
    if (!candidate?.agent_id || seen.has(candidate.agent_id)) continue;
    merged.push(candidate);
    seen.add(candidate.agent_id);
  }

  return merged;
}

function buildOutputText(result) {
  const candidates = result.candidates || [];
  const lines = [
    result.assistant_text || "(no assistant text)"
  ];

  if (candidates.length) {
    lines.push("", "Candidates:");
    for (const [index, candidate] of candidates.entries()) {
      lines.push(
        `${index + 1}. ${candidateLabel(candidate)} [${candidate.agent_id}] score=${formatScore(candidate.score)}${candidate.reason ? ` - ${candidate.reason}` : ""}`
      );
    }
  }

  lines.push(
    "",
    "Commands:",
    JSON.stringify(result.commands || [], null, 2),
    "",
    `requires_confirmation: ${Boolean(result.requires_confirmation)}`
  );

  return lines.join("\n");
}

function buildFocusCommands(candidate) {
  return [
    { kind: "scroll", target_agent_id: candidate.agent_id },
    { kind: "highlight", target_agent_id: candidate.agent_id }
  ];
}

function updateNextButton() {
  const hasMultiple = candidateMatches.length > 1;
  nextBtn.style.display = hasMultiple ? "inline-block" : "none";
  nextBtn.disabled = !hasMultiple || activeCandidateIndex >= candidateMatches.length - 1;
}

function renderOutput(extra = "") {
  setOutput(extra ? `${baseOutputText}\n\n${extra}` : baseOutputText);
}

async function focusCandidate(index) {
  if (!candidateMatches.length) return null;

  activeCandidateIndex = Math.max(0, Math.min(index, candidateMatches.length - 1));
  const candidate = candidateMatches[activeCandidateIndex];
  const execResult = await executeCommandsAndWait(buildFocusCommands(candidate));
  updateNextButton();

  renderOutput(
    `Focused candidate ${activeCandidateIndex + 1}/${candidateMatches.length}: ${candidateLabel(candidate)}\n` +
      JSON.stringify(execResult, null, 2)
  );

  return execResult;
}

async function getContext() {
  const res = await chrome.runtime.sendMessage({ type: "GET_CONTEXT" });
  if (!res?.ok) throw new Error(res?.error || "GET_CONTEXT failed");
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

function isAutoSafe(commands, requiresConfirmation) {
  return !requiresConfirmation && commands.every((command) =>
    command.kind === "highlight" ||
    command.kind === "scroll" ||
    command.kind === "scroll_page"
  );
}

function containsPageScroll(commands) {
  return commands.some((command) => command.kind === "scroll_page");
}

async function executeCommandsAndWait(commands) {
  const results = [];

  for (const command of commands) {
    const execResult = await executeCommands([command]);
    results.push(...(execResult?.results || []));

    if (command.kind === "scroll" || command.kind === "scroll_page") {
      await wait(500);
    }

    const lastResult = results[results.length - 1];
    if (lastResult && lastResult.ok === false) {
      break;
    }
  }

  return { results };
}

async function runSearchLoop(userText) {
  let pageContext = await getContext();
  const wholePageMode = pageContext.capture_mode === "whole_page";
  let previousScrollY = pageContext.viewport?.scroll_y ?? 0;

  for (let step = 0; step < MAX_AUTO_SEARCH_STEPS; step += 1) {
    const result = await runTurn(userText, pageContext);

    candidateMatches = mergeCandidates(candidateMatches, result.candidates || []);
    activeCandidateIndex = candidateMatches.length && activeCandidateIndex < 0 ? 0 : activeCandidateIndex;
    baseOutputText = buildOutputText({
      ...result,
      candidates: candidateMatches
    });
    renderOutput();

    updateNextButton();

    const commands = result.commands || [];
    if (!commands.length) {
      if (candidateMatches.length) {
        await focusCandidate(activeCandidateIndex >= 0 ? activeCandidateIndex : 0);
      }
      return;
    }

    if (!isAutoSafe(commands, result.requires_confirmation)) {
      pendingCommands = commands;
      runBtn.style.display = "inline-block";
      return;
    }

    if (wholePageMode && containsPageScroll(commands)) {
      renderOutput("Whole-page screenshots were already captured, so no additional page scrolling is needed.");
      return;
    }

    const execResult = await executeCommandsAndWait(commands);
    renderOutput("Executed:\n" + JSON.stringify(execResult, null, 2));

    if (!containsPageScroll(commands)) {
      return;
    }

    pageContext = await getContext();
    const currentScrollY = pageContext.viewport?.scroll_y ?? previousScrollY;
    if (currentScrollY === previousScrollY) {
      renderOutput(
        "Executed:\n" +
          JSON.stringify(execResult, null, 2) +
          "\n\nReached the end of the page or could not move further."
      );
      return;
    }

    previousScrollY = currentScrollY;
  }

  renderOutput(`Automatic search stopped after ${MAX_AUTO_SEARCH_STEPS} viewport scans.`);
}

sendBtn.addEventListener("click", async () => {
  try {
    const userText = promptEl.value.trim();
    if (!userText) return;

    setOutput("Capturing whole-page screenshots and collecting context...");
    runBtn.style.display = "none";
    nextBtn.style.display = "none";
    nextBtn.disabled = false;
    pendingCommands = [];
    resetMatches();
    await runSearchLoop(userText);
  } catch (e) {
    setOutput(getErrorMessage(e));
  }
});

runBtn.addEventListener("click", async () => {
  try {
    if (!pendingCommands.length) return;

    const execResult = await executeCommandsAndWait(pendingCommands);
    renderOutput("Executed:\n" + JSON.stringify(execResult, null, 2));
    pendingCommands = [];
    runBtn.style.display = "none";
  } catch (e) {
    setOutput(getErrorMessage(e));
  }
});

nextBtn.addEventListener("click", async () => {
  try {
    if (candidateMatches.length < 2) return;
    if (activeCandidateIndex >= candidateMatches.length - 1) {
      renderOutput(`Already at the last verified match (${candidateMatches.length}/${candidateMatches.length}).`);
      updateNextButton();
      return;
    }
    await focusCandidate(activeCandidateIndex + 1);
  } catch (e) {
    setOutput(getErrorMessage(e));
  }
});
