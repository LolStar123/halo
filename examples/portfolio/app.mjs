import { chunks, retrieve, sentences, extractCue } from "./model.mjs";
const $ = (s) => document.querySelector(s),
    esc = (s) =>
        String(s ?? "").replace(
            /[&<>"']/g,
            (c) =>
                ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                })[c],
        );
let packs,
    docs = [],
    matches = [],
    parts = [],
    position = 0,
    history = [],
    stage = "fast",
    latency = "",
    stageToken = 0;
const promptBank = {
    pilot: ["What is blocking launch?", "What is the rollback plan?", "When is the readiness review?"],
    incident: ["What caused the queue backlog?", "How do we stop the retry storm?", "What still needs reconciling?"],
    research: ["How does the strategy avoid look-ahead?", "How will we test outside training?", "Which costs must be included?"],
    handoff: ["What must the verifier check?", "What happens after a failed save?", "What is the file size limit?"],
};
function renderReader() {
    position = Math.min(position, Math.max(0, parts.length - 1));
    $("#sentence").textContent =
        parts[position] ||
        "No matching note yet. Try another question or add a source.";
    $("#position").textContent =
        `${parts.length ? position + 1 : 0} / ${parts.length}`;
    $("#grounding").textContent = matches.length
        ? `${docs.length} notes`
        : "no grounded notes";
    $("#back").disabled = position === 0;
    $("#next").disabled = position >= parts.length - 1;
    window.__halo = {
        ready: true,
        documents: docs.length,
        passages: chunks(docs).length,
        matches: matches.length,
        sentences: parts.length,
        position,
        stage,
        latency,
    };
}
function setCue(text, nextStage = "source passage", nextLatency = "") {
    stage = nextStage;
    latency = nextLatency;
    $("#answer-stage").textContent = nextStage;
    $("#answer-stage").dataset.stage = nextStage;
    $("#latency").textContent = nextLatency ? ` / ${nextLatency}` : "";
    $("#full-answer").textContent =
        text || "No grounded answer in the selected notes.";
    $("#cue").value = text;
    $("#copy-status").textContent = "";
    parts = sentences(text);
    position = 0;
    renderReader();
}
function renderDocs() {
    $("#documents").innerHTML = docs
        .map(
            (d, i) =>
                `<button data-doc="${i}">${esc(d.title)}</button>`,
        )
        .join("");
}
function renderPrompts() {
    const id = $("#pack").value;
    $("#quick-prompts").innerHTML = (promptBank[id] || [])
        .map((prompt) => `<button type="button" data-prompt="${esc(prompt)}">${esc(prompt)}</button>`)
        .join("");
}
function ask() {
    const token = ++stageToken;
    const q = $("#question").value.trim();
    const started = performance.now();
    matches = retrieve(docs, q);
    const retrievalMs = performance.now() - started;
    $("#status").textContent = matches.length
        ? "grounded locally"
        : "no grounded match";
    const fast = matches[0]?.text || "",
        clever = extractCue(matches);
    setCue(
        fast,
        matches.length ? "fast answer" : "no answer",
        `${retrievalMs.toFixed(1)}ms local`,
    );
    if (matches.length > 1)
        setTimeout(() => {
            if (token !== stageToken) return;
            setCue(clever, "clever answer", "1.10s staged");
        }, 1100);
    $("#evidence").innerHTML = matches
        .map(
            (m, i) =>
                `<article class="passage"><h3>${esc(m.source)} / ${esc(m.heading)}</h3><p>${esc(m.text)}</p><button data-read="${i}">read</button><button data-source="${i}">source</button></article>`,
        )
        .join("");
    if (q && !history.includes(q)) {
        history.unshift(q);
        history = history.slice(0, 8);
    }
    $("#history").innerHTML = history
        .map((q, i) => `<button data-question="${i}">${esc(q)}</button>`)
        .join("");
}
function loadPack() {
    const pack = packs.find((p) => p.id === $("#pack").value);
    docs = structuredClone(pack.docs);
    history = [];
    $("#question").value = pack.question;
    $("#note-status").textContent = "";
    renderDocs();
    renderPrompts();
    ask();
}
function showSource(doc) {
    $("#source-title").textContent = doc.title;
    $("#source-text").textContent = doc.text;
    $("#source").showModal();
}
$("#pack").onchange = loadPack;
$("#reset").onclick = loadPack;
$("#question-form").onsubmit = (e) => {
    e.preventDefault();
    ask();
};
$("#quick-prompts").onclick = (e) => {
    const button = e.target.closest("[data-prompt]");
    if (!button) return;
    $("#question").value = button.dataset.prompt;
    ask();
};
$("#back").onclick = () => {
    position--;
    renderReader();
};
$("#next").onclick = () => {
    position++;
    renderReader();
};
$("#copy-answer").onclick = async () => {
    const text = $("#full-answer").textContent.trim();
    try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
        else {
            const copy = document.createElement("textarea");
            copy.value = text;
            document.body.append(copy);
            copy.select();
            const copied = document.execCommand("copy");
            copy.remove();
            if (!copied) throw Error("Copy was declined by the browser.");
        }
        $("#copy-status").textContent = "answer copied";
    } catch {
        $("#copy-status").textContent = "select the full answer to copy it";
    }
};
$("#size").oninput = () =>
    ($("#sentence").style.fontSize = $("#size").value + "px");
$("#apply-cue").onclick = () => {
    stageToken++;
    setCue($("#cue").value, "edited cue");
};
$("#documents").onclick = (e) => {
    const b = e.target.closest("[data-doc]");
    if (b) showSource(docs[Number(b.dataset.doc)]);
};
$("#close-source").onclick = () => $("#source").close();
$("#evidence").onclick = (e) => {
    const read = e.target.closest("[data-read]"),
        source = e.target.closest("[data-source]");
    if (read) {
        stageToken++;
        setCue(matches[Number(read.dataset.read)].text, "source passage");
    }
    if (source)
        showSource(
            docs.find(
                (d) =>
                    d.title === matches[Number(source.dataset.source)].source,
            ),
        );
};
$("#history").onclick = (e) => {
    const b = e.target.closest("[data-question]");
    if (b) {
        $("#question").value = history[Number(b.dataset.question)];
        ask();
    }
};
function addDoc(title, text) {
    if (!text.trim()) throw Error("Paste some notes first.");
    if (text.length > 200000)
        throw Error("Keep each document under 200,000 characters.");
    let name = title.trim() || "Untitled notes";
    while (docs.some((d) => d.title === name)) name += " (new)";
    docs.push({ title: name, text });
    renderDocs();
    ask();
    $("#note-status").textContent = `${name} added.`;
}
$("#add-note").onclick = () => {
    try {
        addDoc($("#note-title").value, $("#note-text").value);
        $("#note-text").value = "";
    } catch (e) {
        $("#note-status").textContent = e.message;
    }
};
$("#files").onchange = async (e) => {
    try {
        for (const file of e.target.files) {
            if (file.size > 250000)
                throw Error(`${file.name}: choose a text file under 250 KB.`);
            addDoc(file.name, await file.text());
        }
    } catch (e) {
        $("#note-status").textContent = e.message;
    }
};
$("#export").onclick = () => {
    const content =
        docs.map((d) => "# " + d.title + "\n\n" + d.text).join("\n\n---\n\n") +
        "\n\n# Current question\n\n" +
        $("#question").value +
        "\n\n# Reading cue\n\n" +
        $("#cue").value;
    const url = URL.createObjectURL(
        new Blob([content], { type: "text/markdown" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "halo-meeting-notes.md";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
};
document.onkeydown = (e) => {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || $("#source").open)
        return;
    if (e.key === "ArrowRight" && !$("#next").disabled) {
        e.preventDefault();
        $("#next").click();
    }
    if (e.key === "ArrowLeft" && !$("#back").disabled) {
        e.preventDefault();
        $("#back").click();
    }
};
try {
    const r = await fetch("data/meetings.json");
    if (!r.ok) throw Error("Example notes could not load");
    packs = (await r.json()).packs;
    $("#pack").innerHTML = packs
        .map((p) => `<option value="${p.id}">${esc(p.title)}</option>`)
        .join("");
    loadPack();
} catch (e) {
    $("#status").textContent = e.message;
    throw e;
}
