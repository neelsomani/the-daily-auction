const state = {
  wallet: null,
  publicKey: null,
};

const walletStatus = document.getElementById("walletStatus");
const connectBtn = document.getElementById("connectBtn");
const instructionInput = document.getElementById("instruction");
const editBtn = document.getElementById("editBtn");
const deployBtn = document.getElementById("deployBtn");
const nukeBtn = document.getElementById("nukeBtn");
const refreshBtn = document.getElementById("refreshBtn");
const previewFrame = document.getElementById("previewFrame");
const logEl = document.getElementById("log");
const creditStatus = document.getElementById("creditStatus");
const previewActions = refreshBtn ? refreshBtn.closest(".actions") : null;
const editActions = editBtn ? editBtn.closest(".actions") : null;
let historyTimer = null;
let creditTimer = null;
let editInFlight = false;
const editLabel = editBtn ? editBtn.textContent : "Send Edit";
const refreshLabel = refreshBtn ? refreshBtn.textContent : "Refresh Preview";
let lastHistorySignature = null;

function log(message) {
  const time = new Date().toISOString();
  logEl.textContent += `\n[${time}] ${message}`;
  logEl.textContent = logEl.textContent.trimStart();
  logEl.scrollTop = logEl.scrollHeight;
}

function refreshPreview() {
  if (!previewFrame) {
    return;
  }
  const base = previewFrame.dataset.base || previewFrame.src;
  let url;
  try {
    url = new URL(base);
  } catch (err) {
    url = new URL(previewFrame.src, window.location.href);
  }
  url.searchParams.set("t", Date.now().toString());
  previewFrame.src = url.toString();
}

function getProvider() {
  if (window.solana && window.solana.isPhantom) {
    return window.solana;
  }
  return null;
}

function shortenKey(key) {
  if (!key) return "";
  return `${key.slice(0, 4)}…${key.slice(-4)}`;
}

function updateWalletUi() {
  if (state.publicKey) {
    connectBtn.textContent = shortenKey(state.publicKey);
    editBtn.disabled = false;
    deployBtn.disabled = false;
    nukeBtn.disabled = false;
    refreshBtn.disabled = false;
    instructionInput.disabled = false;
    editBtn.textContent = editLabel;
    refreshBtn.textContent = refreshLabel;
    editBtn.removeAttribute("title");
    deployBtn.removeAttribute("title");
    nukeBtn.removeAttribute("title");
    refreshBtn.removeAttribute("title");
    instructionInput.removeAttribute("title");
    previewActions?.classList.remove("actions--disabled");
    editActions?.classList.remove("actions--disabled");
    if (previewActions) previewActions.removeAttribute("data-tooltip");
    if (editActions) editActions.removeAttribute("data-tooltip");
    if (creditStatus) {
      creditStatus.textContent = "Credits: loading...";
    }
    startCreditPolling();
  } else {
    connectBtn.textContent = "Connect Wallet";
    editBtn.disabled = true;
    deployBtn.disabled = true;
    nukeBtn.disabled = true;
    refreshBtn.disabled = true;
    instructionInput.disabled = true;
    editBtn.textContent = editLabel;
    refreshBtn.textContent = refreshLabel;
    editBtn.title = "Connect your wallet to edit";
    deployBtn.title = "Connect your wallet to deploy";
    nukeBtn.title = "Connect your wallet to nuke";
    refreshBtn.title = "Connect your wallet to refresh";
    instructionInput.title = "Connect your wallet to enter instructions";
    previewActions?.classList.add("actions--disabled");
    editActions?.classList.add("actions--disabled");
    if (previewActions) previewActions.setAttribute("data-tooltip", "Connect your wallet to use preview controls");
    if (editActions) editActions.setAttribute("data-tooltip", "Connect your wallet to send edits");
    stopCreditPolling();
    if (creditStatus) {
      creditStatus.textContent = "Connect wallet to see credits";
    }
  }
}

async function connectWallet() {
  const provider = getProvider();
  if (!provider) {
    log("Phantom wallet not found. Install it to continue.");
    return;
  }
  if (state.wallet && state.publicKey) {
    try {
      await provider.disconnect();
    } catch (err) {
      log(`Wallet disconnect failed: ${err.message || err}`);
    }
    state.wallet = null;
    state.publicKey = null;
    updateWalletUi();
    log("Wallet disconnected.");
    return;
  }
  try {
    const resp = await provider.connect();
    state.wallet = provider;
    state.publicKey = resp.publicKey.toString();
    updateWalletUi();
    log("Wallet connected.");
  } catch (err) {
    log(`Wallet connection failed: ${err.message || err}`);
  }
}

function bufferToHex(buffer) {
  const bytes = new Uint8Array(buffer);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function randomNonce() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return bufferToHex(bytes);
}

async function sha256Hex(text) {
  const data = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return bufferToHex(digest);
}

function base64Encode(bytes) {
  if (bytes instanceof ArrayBuffer) {
    bytes = new Uint8Array(bytes);
  }
  if (Array.isArray(bytes)) {
    bytes = Uint8Array.from(bytes);
  }
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

async function signPayload(path, body) {
  if (!state.wallet || !state.wallet.signMessage) {
    throw new Error("Wallet not connected or signMessage unsupported.");
  }
  const nonce = randomNonce();
  const expiry = Math.floor(Date.now() / 1000) + 300;
  const bodyText = JSON.stringify(body);
  const bodyHash = await sha256Hex(bodyText);
  const message = `${state.publicKey}:${nonce}:${expiry}:${path}:${bodyHash}`;
  const encoded = new TextEncoder().encode(message);
  const signed = await state.wallet.signMessage(encoded, "utf8");
  if (window.DEBUG_SIGNATURE) {
    const hasSignature = Boolean(signed && signed.signature);
    const raw = hasSignature ? signed.signature : signed;
    const rawType = raw && raw.constructor ? raw.constructor.name : typeof raw;
    const rawLength = raw && raw.length !== undefined ? raw.length : "n/a";
    console.log("signMessage result", signed);
    log(`debug: signMessage type=${rawType} length=${rawLength} hasSignature=${hasSignature}`);
  }
  let signatureBytes = signed && signed.signature ? signed.signature : signed;
  const signature = `base64:${base64Encode(signatureBytes)}`;

  return {
    headers: {
      "Content-Type": "application/json",
      "X-Wallet": state.publicKey,
      "X-Nonce": nonce,
      "X-Expiry": String(expiry),
      "X-Signature": signature,
    },
    bodyText,
  };
}

async function postCommand(path, body) {
  if (!state.publicKey) {
    log("Connect wallet first.");
    return;
  }
  try {
    if (path === "/edit") {
      editInFlight = true;
      editBtn.disabled = true;
      editBtn.textContent = "Working...";
      instructionInput.disabled = true;
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Publishing...";
    }
    const apiBase = window.location.hostname === "localhost" ? "http://localhost:8080" : "https://api.thedailyauction.com";
    const { headers, bodyText } = await signPayload(path, body);

    const response = await fetch(`${apiBase}${path}`, {
      method: "POST",
      headers,
      body: bodyText,
    });
    const json = await response.json();
    if (!response.ok) {
      log(`Error ${response.status}: ${JSON.stringify(json)}`);
      return;
    }
    log(`${path} ok: ${JSON.stringify(json)}`);
    if (path === "/edit") {
      setTimeout(refreshPreview, 1500);
      refreshCredits();
    }
    if (path === "/nuke") {
      setTimeout(refreshPreview, 1500);
    }
  } catch (err) {
    log(`Request failed: ${err.message || err}`);
  } finally {
    if (path === "/edit") {
      editInFlight = false;
      updateWalletUi();
    }
  }
}

function renderHistory(items) {
  const summarize = (value, limit = 180) => {
    if (!value) return null;
    const compact = value.replace(/\s+/g, " ").trim();
    return compact.length > limit ? `${compact.slice(0, limit)}…` : compact;
  };
  const lines = items.map((item) => {
    const time = new Date(item.timestamp * 1000).toISOString();
    if (item.type === "edit_request") {
      const instruction = item.instruction || "";
      return `[${time}] edit requested: ${summarize(instruction) || ""}`.trim();
    }
    if (item.type === "edit_complete") {
      return `[${time}] edit complete: ${item.status}`;
    }
    if (item.type === "edit_error") {
      return `[${time}] edit error (${item.status}): ${item.response || ""}`.trim();
    }
    if (item.type === "edit_debug") {
      const parts = [
        `bytes=${item.bytes_written || 0}`,
        `changed=${item.changed ? "yes" : "no"}`,
        item.original_hash ? `orig=${item.original_hash}` : null,
        item.updated_hash ? `updated=${item.updated_hash}` : null,
        item.codex_stdout_len ? `stdout_len=${item.codex_stdout_len}` : null,
        item.codex_stderr_len ? `stderr_len=${item.codex_stderr_len}` : null,
        item.html_length ? `html_len=${item.html_length}` : null,
        item.codex_stdout ? `stdout="${summarize(item.codex_stdout)}"` : null,
        item.codex_stderr ? `stderr="${summarize(item.codex_stderr)}"` : null,
      ].filter(Boolean);
      return `[${time}] edit debug: ${parts.join(" ")}`.trim();
    }
    if (item.type === "deploy") {
      return `[${time}] deploy (${item.status}): ${item.response || ""}`.trim();
    }
    if (item.type === "nuke") {
      return `[${time}] nuke (${item.status}): ${item.response || ""}`.trim();
    }
    return `[${time}] ${item.type}`;
  });
  logEl.textContent = lines.join("\n");
  logEl.scrollTop = logEl.scrollHeight;
}

async function refreshHistory() {
  try {
    const apiBase = window.location.hostname === "localhost" ? "http://localhost:8080" : "https://api.thedailyauction.com";
    const response = await fetch(`${apiBase}/history?limit=200`);
    if (!response.ok) {
      log(`History fetch failed: ${response.status}`);
      return;
    }
    const payload = await response.json();
    const items = payload.items || [];
    const signature = JSON.stringify(items.map((item) => [item.timestamp, item.type, item.status]));
    if (signature !== lastHistorySignature) {
      renderHistory(items);
      lastHistorySignature = signature;
    }
  } catch (err) {
    log(`History error: ${err.message || err}`);
  }
}

function startHistoryPolling() {
  if (historyTimer) {
    clearInterval(historyTimer);
  }
  refreshHistory();
  historyTimer = setInterval(refreshHistory, 2000);
}

function stopCreditPolling() {
  if (creditTimer) {
    clearInterval(creditTimer);
    creditTimer = null;
  }
}

async function refreshCredits() {
  if (!state.publicKey || !creditStatus) {
    return;
  }
  try {
    const apiBase = window.location.hostname === "localhost" ? "http://localhost:8080" : "https://api.thedailyauction.com";
    const bodyText = JSON.stringify({ wallet: state.publicKey });
    const response = await fetch(`${apiBase}/credits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyText,
    });
    if (!response.ok) {
      const body = await response.text();
      const detail = body ? ` (${response.status})` : ` (${response.status})`;
      creditStatus.textContent = `Credits unavailable${detail}`;
      return;
    }
    const payload = await response.json();
    creditStatus.textContent = `Credits: ${payload.remaining}/${payload.max}`;
  } catch (err) {
    creditStatus.textContent = "Credits unavailable";
  }
}

function startCreditPolling() {
  stopCreditPolling();
  refreshCredits();
  creditTimer = setInterval(refreshCredits, 15000);
}


connectBtn.addEventListener("click", connectWallet);
editBtn.addEventListener("click", () => {
  if (editInFlight) {
    return;
  }
  const instruction = instructionInput.value.trim();
  if (!instruction) {
    log("Instruction required.");
    return;
  }
  instructionInput.value = "";
  postCommand("/edit", { instruction });
});

deployBtn.addEventListener("click", () => {
  postCommand("/deploy", {});
});

nukeBtn.addEventListener("click", () => {
  if (!confirm("Are you sure? This will reset the site everywhere.")) {
    return;
  }
  postCommand("/nuke", {});
});

refreshBtn.addEventListener("click", () => {
  refreshPreview();
});

window.addEventListener("load", () => {
  startHistoryPolling();
  refreshPreview();
  const provider = getProvider();
  if (provider && provider.isConnected) {
    state.wallet = provider;
    state.publicKey = provider.publicKey?.toString() || null;
    updateWalletUi();
  }
  updateWalletUi();
});
