// System-prompt instructions of the playground, extracted VERBATIM from
// PlaygroundPage.tsx (module-scope strings, no behaviour change).
//
// Order and VOLUME matter in the composed prompt: measured on 12 vague
// requests, the model asks a question 11 times out of 12 with the questions
// instruction alone, 7 times out of 12 when adding naming and editing, and 3
// out of 12 if the questions instruction goes last. Keep ASK first.

// Appended to the system prompt so the model can ask the user one or several
// multiple-choice clarifying questions (rendered as selectable answers, submitted
// together) instead of guessing — the same idea as Claude's "ask the user" tool.
export const ASK_INSTRUCTION = `When you need the user to clarify things before you can answer well, ask your questions as a single fenced block. Output it exactly like this:
\`\`\`ask
{"questions": [{"question": "<question 1>", "options": ["<option>", "<option>"]}, {"question": "<question 2>", "options": ["<option>", "<option>", "<option>"]}]}
\`\`\`
Strict rules:
- Before the block you MAY write ONE short introductory sentence (e.g. "Bien sûr ! Quelques précisions pour bien t'aider :"). Do NOT write the questions or their options as normal text anywhere — they go INSIDE the block ONLY.
- Ask as many questions as are genuinely useful — two if two are enough, more if the request really needs it. Do not pad to reach a number, and do not drop a question that matters. Each question gets 2 to 6 short options in the user's language. Ask everything you need in this one block (the user answers them all at once).
- Do NOT add an "Other" option (the interface adds one).
- The user can pick SEVERAL options for the same question, so write options that can be combined rather than mutually exclusive ones whenever that makes sense. Their answer may come back as "A + B".
- Ask AT MOST ONCE. As soon as the user has answered, you MUST give your real, complete answer using their choices — NEVER reply with another ask block once they have answered.
- The fence language MUST be \`ask\` — never \`json\` or anything else, or the user will not see clickable questions.
- Only ask when it genuinely helps; otherwise just answer normally.`;

// Instruction added to the system prompt: fix an already produced file without
// rewriting it entirely. Rewriting 400 lines to change three costs time,
// tokens, and reintroduces errors elsewhere in the file.
// The model names its own files: it knows what the user asked for
// and which project they fit into. Without this, the UI has to guess and
// falls back to a generic name.
export const NAME_INSTRUCTION = `Name every file you output: put its path in backticks on the line just before the code block (\`index.html\`, \`roles/web/tasks/main.yml\`), chosen from what the user asked. Reuse the exact same name for a file you already produced.`;

// The partial-edit protocol was removed: on generated code containing
// errors, a spot fix left a half-right file, and
// a mis-copied anchor did not apply at all. We ask for the file
// ENTIRE — it is longer to generate, but what comes out is usable as is.
// Only for when no file tools are offered: the server arms str_replace on an
// edit request, and this instruction then made the model skip them and
// reprint the whole file (2026-10-12) — hence the explicit precedence.
export const REWRITE_INSTRUCTION = `When the user asks you to fix or change a file you already produced: if file-editing tools (str_replace, view_file) are available, use them and do not print the file again. Otherwise, output that file COMPLETE, from its first line to its last, under the exact same name — never a partial file, an excerpt, a diff, or a "rest unchanged" placeholder.`;

// The model GIVES UP on its own on a large file: measured in prod on 22/08,
// it stopped at 14 187 tokens out of 131 072 available, with
// finish_reason=stop (so nothing distinguished it from a successful answer),
// writing « Le fichier est trop long pour etre affiche en entier ici » followed
// by a truncated file presented as complete. No technical limit was
// reached. This instruction goes out on EVERY turn, not only when a
// file already exists, and goes LAST so as not to dilute ASK_INSTRUCTION
// (whose effectiveness depends on its position at the top, see measurement below).
// Voice mode: the answer is READ ALOUD sentence by sentence (VoiceMode).
// Written-chat habits sound absurd spoken — a bullet list, a code block, a
// URL spelled out — and a long answer keeps the user waiting to talk.

export const COMPLETENESS_INSTRUCTION = `Never abridge a file you were asked to produce. Never write that a file is "too long to show here", never say you are giving a "shortened", "simplified" or "essential" version, and never replace any part of a file with an ellipsis, a placeholder, or a comment such as "rest of the code unchanged". There is no display limit: write the file in full, from its first line to its last. If you run out of room before the end, stop mid-file rather than closing it early — you will be asked to continue, and you will resume at the exact character where you stopped. A truncated file presented as complete is the worst possible answer.`;
