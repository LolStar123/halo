export const defaults = {
  question: "How did you improve tube reliability research?",
  sentence: 0,
  notes: [
    {
      id: "tube",
      keywords: ["tube", "reliability", "transport", "data"],
      text: "I logged Tube service snapshots in SQLite. Replaying delays and recoveries produced an Elo-style reliability table. The source history stayed available beside the rankings.",
    },
    {
      id: "baxter",
      keywords: ["ai", "agents", "workflow", "verification"],
      text: "I built Baxter to triage requests and coordinate coding work. Each task declared the files it could change. An independent check determined whether the work was complete.",
    },
    {
      id: "hardware",
      keywords: ["hardware", "auction", "gpu", "risk", "fees"],
      text: "I compared auction lots using condition and resale evidence. Buyer fees, VAT and fault risk went into the maximum bid. That kept the headline price separate from the amount I could actually afford to pay.",
    },
  ],
};
export const controls = [
  { key: "question", label: "Question", type: "text" },
  {
    key: "sentence",
    label: "Sentence to read (starts at 0)",
    type: "number",
    min: 0,
    max: 12,
    step: 1,
  },
];
export function retrieve(question, notes) {
  const terms = [...new Set(question.toLowerCase().match(/[a-z]+/g) || [])];
  return notes
    .map((n) => ({
      ...n,
      score: n.keywords.filter((k) => terms.includes(k)).length,
    }))
    .filter((n) => n.score > 0)
    .sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
}
export function sentences(text) {
  return (text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [])
    .map((s) => s.trim())
    .filter(Boolean);
}
export function run(i) {
  const found = retrieve(i.question, i.notes),
    parts = found.length ? sentences(found[0].text) : [],
    index = Math.min(
      Math.max(0, Math.trunc(i.sentence)),
      Math.max(0, parts.length - 1),
    );
  return {
    summary:
      parts[index] ||
      "No matching evidence. Add a relevant note before answering.",
    metrics: {
      "matching notes": found.length,
      "reading sentence": parts.length
        ? `${index + 1} / ${parts.length}`
        : "none",
      source: found[0]?.id || "none",
    },
    columns: ["note", "matched keywords", "prepared reference"],
    rows: found.map((n) => [n.id, n.score, n.text]),
    steps: [
      "Receive the question",
      "Retrieve relevant prepared notes",
      "Keep the source next to the cue",
      "Advance through complete sentences",
    ],
    artifact: { evidence: found, parts, index },
  };
}
