# General Page Types and Layout Selection Manual

This manual guides the Agent to automatically choose layouts based on the content task. Do not fix the chapter structure; the number of modules, page order, and page types are all determined by the user's topic, page count, materials, and presentation context.

## 1. Page Planning Principles

- **Content first**: first clarify the problem each page must solve, then choose the layout.
- **Adaptive structure**: 2–6 modules are all acceptable; short PPTs may have no section pages, long PPTs may use transition pages.
- **Diverse expression**: avoid using the same layout for more than 2 consecutive pages.
- **Completeness first**: each page has at least a title, supporting information, visual structure, and footer.

## 2. Page Types

### Cover Page

- Applicable: the starting page of every PPT.
- Layout: full-bleed dark background or image background + dark overlay; large centered title; short tag above; thin lines and diamonds/dots on left and right; corner marks at the four corners; author/date/page number at the bottom.
- Content: the main title expresses the topic; the subtitle uses a "subject · problem · value" structure.
- Function: `add_cover_slide()`.

### Table of Contents Page

- Applicable: page count ≥ 6 or multiple content modules exist.
- Layout: 2×2 large cards preferred; each card contains a module tag, module name, brief description, key pages, and page-number range.
- Content: show only the actual structure; reduce cards when there are few modules, and merge to display core modules when there are many.
- Function: `add_toc_slide()`.

### Section Page / Transition Page

- Applicable: module switching, narrative turning points, and pacing adjustment between information-dense pages.
- Layout: large numbering/keyword watermark + centered or lower-left title + short subtitle + minimal line decoration.
- Content: explain the question that the next group of pages will answer; do not write vague section names.
- Function: `add_section_slide()`.

### Viewpoint Statement Page

- Applicable: defining a core judgment, insight, principle, or strategic proposition.
- Layout: title bar at the top + centered large viewpoint/quote-style expression + 2–3 evidence cards at the bottom.
- Content: one main judgment + 2–4 pieces of evidence/explanation.
- Function: `layout_quote()`, `layout_focus_statement()`, `layout_bullet_list()`.

### Image-Text Mixed Page

- Applicable: cases, scenarios, architectures, maps, user journeys, and product UI descriptions.
- Layout: image on the left with a 2×2 info card grid on the right, or image on the right with descriptions on the left; place the image in a dark container and add a caption bar.
- Content: the image illustrates the subject/scenario; the cards on the right carry metrics, observations, risks, and value.
- Function: `layout_image_left_grid()`.

### Multi-Card Information Page

- Applicable: capability highlights, core findings, solution pillars, risk categories, training key points.
- Layout: three-column large cards or 2×2 cards; each card has an accent-colored top edge, number, title, and short description.
- Content: 1 viewpoint per card, 2–4 short sentences; do not pile up long text.
- Function: `layout_three_cols()`, `layout_cards_grid()`, `layout_kpi_2x2()`.

### Left-Right Columns Page

- Applicable: problem-solution, status-goal, subject grouping-conclusion, input-output.
- Layout: list/subject pool on the left, conclusion/recommendation/impact cards on the right; keep a clear divider in the middle.
- Content: facts or categories on the left, judgments and actions on the right.
- Function: `layout_two_panels()`, `layout_value_split()`.

### Top-Bottom Partition Page

- Applicable: metric overview on top, cause breakdown below; workflow on top, conclusion below.
- Layout: a metric bar or title band at the top, with cards/tables/workflow below.
- Content: give the numbers or conclusion first, then explain the support.
- Function: `layout_metrics_strip()` + other layout combinations.

### Key Conclusion Page

- Applicable: module wrap-up, solution summary, executive conclusion.
- Layout: a centered conclusion card + 3–5 supporting points around it; a takeaway at the bottom.
- Content: the conclusion must be actionable or judgeable; avoid empty talk.
- Function: `layout_conclusion()`, `layout_value_split()`.

### Data Display Page

- Applicable: metric dashboards, scale, trends, comparisons, performance, survey results.
- Layout: a metric bar at the top + KPI cards + tables/descriptions; a few key numbers shown large in gilded color.
- Content: when there is no real data, use clearly replaceable placeholders and note "can be replaced with actual data".
- Function: `layout_metrics_strip()`, `layout_kpi_2x2()`, `layout_table()`.

### Workflow Page

- Applicable: implementation paths, workflows, technical pipelines, project advancement, training steps.
- Layout: a small-node timeline at the top + stage cards below + a takeaway at the bottom.
- Content: 3–5 steps is best; each step includes input, action, output, or ownership.
- Function: `layout_workflow()`.

### Timeline Page

- Applicable: roadmaps, plans, evolution, research processes, event sequences, milestones.
- Layout: a horizontal timeline with nodes staggered above and below; KPI/milestone cards may be added at the bottom.
- Content: no more than 6 nodes; highlight the current stage or key turning point.
- Function: `layout_timeline()`.

### Comparison Analysis Page

- Applicable: option A/B, competitor comparison, before-after change, opportunities and risks, differences between subjects.
- Layout: left-right comparison cards + a central conclusion/arrow; or a table matrix.
- Content: consistent dimensions, clear conclusion; do not just list items.
- Function: `layout_comparison()`, `layout_table()`.

### Case Study Page

- Applicable: customer cases, project samples, best practices, problem retrospectives.
- Layout: case background/image on the left, challenge-action-result cards on the right, an insight at the bottom.
- Content: organize as "scenario—approach—result—reusable lesson".
- Function: `layout_case_study()`, `layout_image_left_grid()`.

### Quote Page

- Applicable: emphasizing a viewpoint, expert/customer voices, training catchphrases, module transitions.
- Layout: large quotation marks + large-font quote + source; keep the background dark with whitespace.
- Content: the quote should not exceed 2 lines; may be accompanied by 1 explanatory sentence.
- Function: `layout_quote()`.

### Summary Page

- Applicable: wrapping up the whole deck, recommendations, action lists, next steps.
- Layout: a five-point summary or 3–4 action cards; a clear next step at the bottom.
- Content: tie back to the user's goals and give actionable recommendations.
- Function: `layout_conclusion()`, `layout_bullet_list()`, `layout_value_split()`.

### Ending Page

- Applicable: the last page.
- Layout: echo the cover; a centered acknowledgment/closing statement; may include contact info, project period, and a QR code placeholder.
- Content: do not introduce new arguments; keep it concise.
- Function: `add_ending_slide()`.

## 3. Layout Pacing Recommendations

- 8 pages: cover, table of contents, 3–4 core content pages, summary, ending.
- 12 pages: cover, table of contents, 8–9 content pages, summary/action, ending.
- 18 pages: cover, table of contents, 3–5 modules with 3–4 pages each, with 1–2 transition/visual pages inserted in between, ending.
- Training scenarios use more workflow, case, and key-point cards; reporting scenarios use more data, comparison, and conclusion; solution scenarios use more problem-solution, path, resources, and milestones; research scenarios use more method, findings, evidence, limitations, and conclusion.

## 4. Layout Selector Abstracted from the Reference Image Decks

- **Need a strong ceremonial opening/closing**: choose the cover/ending page, full-bleed dark image or dark texture + centered title + corner marks at the four corners.
- **Need to explain the overall structure**: choose a 2×2 table-of-contents card layout, with at most 3 sub-pages per card.
- **Need to explain stage evolution**: choose a horizontal timeline, supplemented by 3–4 KPI or milestone cards at the bottom.
- **Need to explain one core concept**: choose a circular badge on the left + stacked info bars on the right.
- **Need to show three parallel pieces of information**: choose three vertical columns of cards, with colored thin lines on top and short tags at the bottom.
- **Need scenario/case evidence**: choose an image on the left with cards on the right, or a full-bleed image background + a metric bar below.
- **Need high-density executive information**: choose a metric bar on top + a multi-panel matrix below.
- **Need route/network/ecosystem relationships**: choose a path diagram/map-style horizontal nodes + category cards below.
- **Need to summarize causes or value**: choose a large keyword/circular badge on the left + 4–6 conclusion cards on the right.
