const state = {
  wallet: null,
  publicKey: null,
};

const walletStatus = document.getElementById("walletStatus");
const connectBtn = document.getElementById("connectBtn");
const apiBaseInput = document.getElementById("apiBase");
const instructionInput = document.getElementById("instruction");
const editBtn = document.getElementById("editBtn");
const deployBtn = document.getElementById("deployBtn");
const nukeBtn = document.getElementById("nukeBtn");
const refreshBtn = document.getElementById("refreshBtn");
const previewFrame = document.getElementById("previewFrame");
const logEl = document.getElementById("log");
let historyTimer = null;
let editInFlight = false;

function log(message) {
  const time = new Date().toISOString();
  logEl.textContent = `[${time}] ${message}\n` + logEl.textContent;
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

async function connectWallet() {
  const provider = getProvider();
  if (!provider) {
    log("Phantom wallet not found. Install it to continue.");
    return;
  }
  try {
    const resp = await provider.connect();
    state.wallet = provider;
    state.publicKey = resp.publicKey.toString();
    walletStatus.textContent = state.publicKey;
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

function base58Encode(bytes) {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let digits = [0];
  for (let i = 0; i < bytes.length; i += 1) {
    let carry = bytes[i];
    for (let j = 0; j < digits.length; j += 1) {
      const value = digits[j] * 256 + carry;
      digits[j] = value % 58;
      carry = Math.floor(value / 58);
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = Math.floor(carry / 58);
    }
  }
  for (let k = 0; k < bytes.length && bytes[k] === 0; k += 1) {
    digits.push(0);
  }
  return digits.reverse().map((d) => alphabet[d]).join("");
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
  const signatureBytes = await state.wallet.signMessage(encoded, "utf8");
  const signature = base58Encode(signatureBytes);

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
      refreshBtn.disabled = true;
    }
    const apiBase = apiBaseInput.value.trim().replace(/\/$/, "");
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
    }
  } catch (err) {
    log(`Request failed: ${err.message || err}`);
  } finally {
    if (path === "/edit") {
      editInFlight = false;
      editBtn.disabled = false;
      refreshBtn.disabled = false;
    }
  }
}

function renderHistory(items) {
  const lines = items.map((item) => {
    const time = new Date(item.timestamp * 1000).toISOString();
    if (item.type === "edit_request") {
      return `[${time}] edit requested: ${item.instruction || ""}`.trim();
    }
    if (item.type === "edit_complete") {
      return `[${time}] edit complete: ${item.status}`;
    }
    if (item.type === "edit_error") {
      return `[${time}] edit error (${item.status}): ${item.response || ""}`.trim();
    }
    if (item.type === "deploy") {
      return `[${time}] deploy (${item.status}): ${item.response || ""}`.trim();
    }
    if (item.type === "nuke") {
      return `[${time}] nuke (${item.status}): ${item.response || ""}`.trim();
    }
    return `[${time}] ${item.type}`;
  });
  logEl.textContent = lines.reverse().join("\n");
}

async function refreshHistory() {
  try {
    const apiBase = apiBaseInput.value.trim().replace(/\/$/, "");
    const response = await fetch(`${apiBase}/history?limit=200`);
    if (!response.ok) {
      log(`History fetch failed: ${response.status}`);
      return;
    }
    const payload = await response.json();
    renderHistory(payload.items || []);
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

function setDefaultApiBase() {
  if (!apiBaseInput) {
    return;
  }
  if (window.location.hostname === "localhost") {
    apiBaseInput.value = "http://localhost:8080";
    return;
  }
  const prod = apiBaseInput.dataset.prod;
  if (prod) {
    apiBaseInput.value = prod;
  }
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
  setDefaultApiBase();
  startHistoryPolling();
  const provider = getProvider();
  if (provider && provider.isConnected) {
    state.wallet = provider;
    state.publicKey = provider.publicKey?.toString() || null;
    if (state.publicKey) {
      walletStatus.textContent = state.publicKey;
    }
  }
});
