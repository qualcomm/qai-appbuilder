---
name: Read arXiv Paper
description: Fetch and summarize academic papers from arxiv.org. Use for arxiv.org paper URLs (e.g. arxiv.org/abs/...). Triggers: 'read/summarize this arxiv paper', 'read paper', 'summarize paper'. NOTE: arxiv URLs must use this skill, NOT summarizenews.
tags: arxiv, paper, research, summarize
use_for: Reading arxiv papers, summarizing research papers, extracting paper key points
---

## Instructions

Given an arXiv URL, you MUST call the `exec` tool immediately. Do NOT describe the steps — just call the tool.

**Step 1**: Extract the arxiv ID from the URL (e.g. from `https://arxiv.org/abs/1706.03762` → ID is `1706.03762`).

**Step 2**: Call `exec` with this exact command (replace `{arxiv_id}` with the actual ID):

```
python ${SKILL_DIR}/scripts/fetch_arxiv.py {arxiv_id}
```

**Step 3**: Use the script output as the paper content and produce a summary covering: title/authors, problem, contributions, method, results, takeaways.

## Example

User: Please read this paper https://arxiv.org/abs/1706.03762

You MUST call exec immediately:
<tool_call>
{"name": "exec", "arguments": {"command": "python ${SKILL_DIR}/scripts/fetch_arxiv.py 1706.03762"}}
</tool_call>
