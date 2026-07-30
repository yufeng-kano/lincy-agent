# Memory Maintenance Rules

Rules for reorganizing memory files. Follow them strictly.

**IMPORTANT: Only modify the file(s) explicitly specified in the task.
Do not scan, check, or edit any other files. Read other files only
if needed to understand context for the target file.**

## General Rules

- All memory content must be in Traditional Chinese (繁體中文)
- No emoji allowed in any memory file
- Do not delete any content unless explicitly asked — only reorganize
- When removing duplicates, keep the more complete version
- Do not modify files under kernel/ directory
- Edit files with read_file / edit_file / write_file only; do not use
  shell commands. write_file cannot overwrite a non-empty file — use
  edit_file for existing content.

## File Format Rules

### index.md
- Format: `- [display name](relative-path) — one-line description`
- Paths must be relative to the index.md location
- Descriptions in Traditional Chinese
- Do not use table format (exception: people/index.md)
- Do not add free-text paragraphs or notes

### temp-memory.md
- Format: `- [YYYY-MM-DD HH:MM] content`
- Objective facts use real names, subjective feelings use pet names
- One entry per line

### long-term.md
- Section `## 解讀原則`: fixed interpretation preamble — do not modify,
  reorder, or reformat it
- Section `## 核心價值`: free-text bullets (`- text`) without dates or
  checkboxes — do not reformat, date, or reorder them
- Section `## 約定`: `- [ ] [YYYY-MM-DD] person: description`
- Section `## 清單`: `- [YYYY-MM-DD] description`
- Section `## 重要記錄`: `- [YYYY-MM-DD] description`
- Keep the `<!-- ... -->` format-hint comments under each heading
- No emoji; no HTML other than those format-hint comments
- Remove completed checkboxes (`- [x]`) older than 7 days

### artifacts.md
- Format: `- [YYYY-MM-DD] [file|creation] title | path: artifacts/... | note: ...`
- `path:` must start with `artifacts/`

### Other archive/deprecated .md files
- Must start with a `# Title` heading
- Use structured sections with `##` headings
- Keep content factual and concise

## Safety

- Never delete index.md files
- Never modify persona.md without explicit instruction
- When splitting a file, ensure all content is preserved in the new location
- After reorganization, verify the parent index.md links are correct
