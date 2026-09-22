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
    history = [];
function renderReader() {
    position = Math.min(position, Math.max(0, parts.length - 1));
    $("#sentence").textContent =
        parts[position] ||
        "No matching note yet. Try another question or add a source.";
    $("#position").textContent =
        `${parts.length ? position + 1 : 0} / ${parts.length}`;
    $("#back").disabled = position === 0;
    $("#next").disabled = position >= parts.length - 1;
    window.__halo = {
        ready: true,
        documents: docs.length,
        passages: chunks(docs).length,
        matches: matches.length,
        sentences: parts.length,
        position,
    };
}
function setCue(text) {
    $("#cue").value = text;
    parts = sentences(text);
    position = 0;
    renderReader();
}
function renderDocs() {
    $("#documents").innerHTML = docs
        .map(
            (d, i) =>
                `<button data-doc="${i}">${esc(d.title)} <small>/ ${chunks([d]).length} passages</small></button>`,
        )
        .join("");
}
function ask() {
    const q = $("#question").value.trim();
    matches = retrieve(docs, q);
    $("#status").textContent = matches.length
        ? `${matches.length} matching passages. Reading text contains direct excerpts, not an AI-written answer.`
        : "No matching evidence in these notes. Add the missing source or try more specific words.";
    setCue(extractCue(matches));
    $("#evidence").innerHTML = matches
        .map(
            (m, i) =>
                `<article class="passage"><h3>${esc(m.source)} / ${esc(m.heading)}</h3><p>${esc(m.text)}</p><button data-read="${i}">read this passage</button><button data-source="${i}">open source</button><small>matched: ${esc(m.matched.join(", "))}</small></article>`,
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
$("#back").onclick = () => {
    position--;
    renderReader();
};
$("#next").onclick = () => {
    position++;
    renderReader();
};
$("#size").oninput = () =>
    ($("#sentence").style.fontSize = $("#size").value + "px");
$("#apply-cue").onclick = () => setCue($("#cue").value);
$("#documents").onclick = (e) => {
    const b = e.target.closest("[data-doc]");
    if (b) showSource(docs[Number(b.dataset.doc)]);
};
$("#close-source").onclick = () => $("#source").close();
$("#evidence").onclick = (e) => {
    const read = e.target.closest("[data-read]"),
        source = e.target.closest("[data-source]");
    if (read) setCue(matches[Number(read.dataset.read)].text);
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
    $("#note-status").textContent = `Added ${name}. Files stay in this tab.`;
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
