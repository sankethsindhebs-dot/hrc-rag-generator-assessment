"use strict";
/* Minimal UI. Every piece of dynamic text (filenames, answers, citations, errors) is inserted with
   textContent / append(string), never as HTML, and there are no inline handlers or scripts. */

const state = { collections: [], selected: null, health: null, healthTimer: null };
const $ = (selector) => document.querySelector(selector);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) throw new Error("event-handler attributes are not allowed");
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child); // strings become text nodes
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  let response;
  try { response = await fetch(path, options); }
  catch { return { ok: false, status: 0, data: { error: { code: "network_error", message: "Could not reach the server." } } }; }
  let data = null;
  if (response.status !== 204) { try { data = await response.json(); } catch { data = null; } }
  if (!response.ok && !(data && data.error)) data = { error: { code: "http_" + response.status, message: "Request failed (HTTP " + response.status + ")." } };
  return { ok: response.ok, status: response.status, data };
}

function errorText(result) { const e = result.data && result.data.error; return e ? e.message + " (" + e.code + ")" : "Request failed."; }

/* ---------- status banners ---------- */
async function loadHealth() {
  const result = await api("GET", "/api/health");
  const health = result.data && result.data.generation ? result.data : null;
  state.health = health;
  const box = $("#banners");
  clear(box);
  if (!health) { box.append(el("p", { class: "banner warn", text: "The server did not report its status." })); return; }
  if (!health.ready) box.append(el("p", { class: "banner info", text: "The embedding model is loading (it downloads on first use). Uploads and questions will work once it is ready." }));
  if (health.generation.configured) box.append(el("p", { class: "banner ok", text: "Claude generation is configured (model: " + health.generation.model + ")." }));
  else box.append(el("p", { class: "banner warn", text: "Claude generation is NOT configured. " + (health.generation.detail || "") + " Retrieval still works, so you will see the matching passages, but no answers." }));
  if (!health.ready && !state.healthTimer) state.healthTimer = setInterval(loadHealth, 3000);
  if (health.ready && state.healthTimer) { clearInterval(state.healthTimer); state.healthTimer = null; }
}

/* ---------- collections ---------- */
async function loadCollections() {
  const result = await api("GET", "/api/collections");
  state.collections = result.ok ? result.data.collections : [];
  if (!result.ok) setCollectionMessage(errorText(result));
  if (state.selected && !state.collections.some((c) => c.id === state.selected)) state.selected = null;
  renderCollections();
  await loadDocuments();
}

function setCollectionMessage(text) { $("#collection-message").textContent = text || ""; }

function renderCollections() {
  const list = $("#collection-list");
  clear(list);
  if (!state.collections.length) list.append(el("li", { class: "muted", text: "No collections yet. Create one above." }));
  for (const c of state.collections) {
    const button = el("button", { type: "button", class: "choice", "aria-pressed": String(c.id === state.selected) },
      c.name, el("span", { class: "muted", text: "  —  " + c.document_count + " document(s), " + c.chunk_count + " chunk(s)" }));
    button.addEventListener("click", () => selectCollection(c.id));
    list.append(el("li", { class: c.id === state.selected ? "selected" : "" }, button));
  }
  const current = state.collections.find((c) => c.id === state.selected);
  $("#selected-name").textContent = current ? "— " + current.name : "";
  for (const id of ["#file-input", "#paste-text", "#question"]) $(id).disabled = !current;
  for (const id of ["#upload-button", "#ask-button", "#delete-button"]) $(id).disabled = !current;
}

async function selectCollection(id) {
  state.selected = id;
  clear($("#result")); clear($("#upload-results")); setCollectionMessage("");
  renderCollections();
  await loadDocuments();
}

async function loadDocuments() {
  const list = $("#document-list");
  clear(list);
  if (!state.selected) { list.append(el("li", { class: "muted", text: "Select a collection to see its documents." })); return; }
  const result = await api("GET", "/api/collections/" + encodeURIComponent(state.selected) + "/documents");
  if (!result.ok) { list.append(el("li", { class: "bad", text: errorText(result) })); return; }
  if (!result.data.documents.length) list.append(el("li", { class: "muted", text: "No documents indexed yet." }));
  for (const d of result.data.documents) list.append(el("li", { class: "ok", text: "✓ " + d.filename + " — indexed (" + d.chunk_count + " chunk(s))" }));
}

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#collection-name");
  const result = await api("POST", "/api/collections", { name: input.value });
  if (!result.ok) { setCollectionMessage(errorText(result)); return; }
  input.value = ""; setCollectionMessage("");
  state.selected = result.data.id;
  await loadCollections();
});

$("#delete-button").addEventListener("click", async () => {
  const current = state.collections.find((c) => c.id === state.selected);
  if (!current || !window.confirm("Delete the collection and all its documents?")) return;
  const result = await api("DELETE", "/api/collections/" + encodeURIComponent(current.id));
  if (!result.ok) { setCollectionMessage(errorText(result)); return; }
  state.selected = null; clear($("#result")); clear($("#upload-results"));
  await loadCollections();
});

/* ---------- uploads ---------- */
$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selected) return;
  const form = new FormData();
  for (const file of $("#file-input").files) form.append("files", file, file.name);
  const pasted = $("#paste-text").value;
  if (pasted.trim()) form.append("files", new File([pasted], $("#paste-name").value || "pasted-text.txt", { type: "text/plain" }));
  const results = $("#upload-results");
  clear(results);
  if (!form.has("files")) { results.append(el("li", { class: "bad", text: "Choose at least one file or paste some text." })); return; }
  $("#upload-button").disabled = true;
  results.append(el("li", { class: "muted", text: "Uploading and indexing…" }));
  const result = await api("POST", "/api/collections/" + encodeURIComponent(state.selected) + "/documents", form);
  clear(results);
  if (result.data && result.data.indexed) {
    for (const d of result.data.indexed) results.append(el("li", { class: "ok", text: "✓ " + d.filename + " — indexed (" + d.chunk_count + " chunk(s))" }));
    for (const r of result.data.rejected) results.append(el("li", { class: "bad", text: "✗ " + r.filename + " — " + r.message }));
  } else results.append(el("li", { class: "bad", text: errorText(result) }));
  $("#file-input").value = ""; $("#paste-text").value = "";
  await loadCollections();
});

/* ---------- asking ---------- */
function sourceBlock(item, labelText) {
  const meta = [labelText, item.filename, "chunk " + item.chunk_index, "score " + item.score.toFixed(3)];
  if (item.used === false) meta.push("not sent to Claude");
  return el("div", { class: "source" + (item.used === false ? " unused" : "") },
    el("div", { class: "meta", text: meta.filter(Boolean).join("  ·  ") }),
    el("blockquote", { text: item.snippet }));
}

function evidenceList(evidence) {
  if (!evidence || !evidence.length) return null;
  const details = el("details", {}, el("summary", { text: "Retrieved evidence (" + evidence.length + ")" }));
  for (const e of evidence) details.append(sourceBlock(e, e.label ? "[" + e.label + "]" : ""));
  return details;
}

const REASONS = {
  empty_collection: "This collection has no documents yet.",
  below_threshold: "No passage was similar enough to the question, so Claude was not called.",
  model_declined: "Claude reviewed the closest passages and found they do not contain the answer.",
  no_valid_citations: "The generated answer did not cite the documents correctly, so it was withheld.",
};

function renderAnswer(data) {
  const box = $("#result");
  clear(box);
  const badges = { answered: ["ok", "Grounded answer"], insufficient_context: ["warn", "Insufficient context"], unverified: ["bad", "Answer withheld"] };
  const [kind, label] = badges[data.status] || ["info", data.status];
  const card = el("div", { class: "card" }, el("span", { class: "badge " + kind, text: label }));
  card.append(el("p", { class: "answer", text: data.answer }));
  if (data.reason && REASONS[data.reason]) card.append(el("p", { class: "explain", text: REASONS[data.reason] }));
  if (data.detail) card.append(el("p", { class: "explain", text: "Claude's note (not an answer): " + data.detail }));
  if (data.citations.length) {
    card.append(el("h3", { text: "Sources cited" }));
    for (const c of data.citations) card.append(sourceBlock(c, "[" + c.label + "]"));
  }
  for (const w of data.warnings) card.append(el("p", { class: "explain", text: "Note: " + w }));
  const evidence = evidenceList(data.evidence);
  if (evidence) card.append(evidence);
  card.append(el("p", { class: "explain", text: "Similarity threshold " + data.threshold + " · Claude " + (data.generator_called ? "was" : "was not") + " called." }));
  box.append(card);
}

function renderAskError(result) {
  const box = $("#result");
  clear(box);
  const error = result.data.error;
  if (error.code === "generation_unavailable") {
    const card = el("div", { class: "card" }, el("span", { class: "badge warn", text: "Claude generation is not configured" }),
      el("p", { class: "answer", text: error.message }),
      el("p", { class: "explain", text: "Retrieval worked. These are the passages that matched your question:" }));
    const evidence = evidenceList(error.evidence);
    if (evidence) { evidence.open = true; card.append(evidence); }
    box.append(card);
    return;
  }
  box.append(el("div", { class: "card" }, el("span", { class: "badge bad", text: "Error" }), el("p", { class: "answer", text: errorText(result) })));
}

$("#ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("#question").value.trim();
  if (!state.selected || !question) return;
  const box = $("#result");
  clear(box);
  box.append(el("p", { class: "muted", text: "Searching and answering…" }));
  $("#ask-button").disabled = true;
  const result = await api("POST", "/api/collections/" + encodeURIComponent(state.selected) + "/ask", { question });
  $("#ask-button").disabled = false;
  if (result.ok) renderAnswer(result.data); else renderAskError(result);
});

loadHealth();
loadCollections();
