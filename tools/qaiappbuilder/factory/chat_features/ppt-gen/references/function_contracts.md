# `pptx_base.py` Key Function Parameter Contracts

You must check this page before using composite layouts to avoid field misalignment that results in half-finished pages.

## Page Foundation Functions

- `init_presentation(export_dir)`: creates a 16:9 PPT.
- `add_gilded_texture(slide, ...)`: general-purpose dark gilded background; compatible alias `add_tang_texture()`.
- `add_section_tag(slide, text, color, ...)`: header module short tag; compatible alias `add_chapter_tag()`.
- `add_deck_title(slide, title, subtitle, ...)`: content page title bar; compatible alias `add_imperial_title()`.
- `add_deck_footer(slide, section_text, num, total)`: footer page number; compatible alias `add_imperial_footer()`.

## `layout_kpi_2x2(slide, kpi_data, x=5.95, y=1.55, w=6.25, h=4.7)`

`kpi_data` has at most 4 items; each item must be a 7-tuple:

```python
(name, val, unit, color, compare, interpret, desc)
```

Field meanings: card title, main value/main label, unit/label description, accent color, supplementary judgment one, supplementary judgment two, description.

## `layout_image_left_grid(slide, image_path, caption, cards, image_box=...)`

The `cards` on the right are passed directly to `layout_kpi_2x2()`, so each item must also be a 7-tuple:

```python
(name, val, unit, color, compare, interpret, desc)
```

Do not pass a free-form structure such as `(title, subtitle, desc, color)`.

## `layout_workflow(slide, stages, y=1.68)`

`stages` is recommended to have 3–5 items; each item must be a 6-tuple:

```python
(step_label, title, color, subtitle, lines, tag)
```

`lines` must be a list, for example `['Action One', 'Action Two']`; do not pass a single string.

## `layout_three_cols(slide, cards_data, start_y=1.55, card_h=4.95)`

Each item is usually:

```python
(icon, title, color, content)
```

`content` is recommended to be a dict:

```python
{"kpi":"XX%", "unit":"Metric description", "desc":"One-sentence explanation", "lines":["Point 1", "Point 2"], "tag":"Short tag", "watermark":"Optional watermark"}
```

## `layout_cards_grid(slide, cards, cols=2, ...)`

Each item is a dict:

```python
{"title":"Card title", "tag":"Short tag", "desc":"Description", "lines":["Point 1", "Point 2"], "color":ACCENT_YEL}
```

Suitable for 4–6 info cards.

## `layout_comparison(slide, left, right, conclusion='', ...)`

`left` and `right` are dicts:

```python
{"title":"Option A", "subtitle":"Positioning", "points":["Advantage", "Limitation"], "color":ACCENT_RED}
```

`conclusion` is the central or bottom conclusion; it must be a judgment specific to this page, not vague.

## `layout_case_study(slide, case_title, context, actions, results, takeaway='')`

- `context`: a list of short sentences for the scenario/background.
- `actions`: a list of short sentences for actions or methods.
- `results`: a list of short sentences for results, benefits, or lessons.
- `takeaway`: a single reusable insight at the bottom.

## `layout_badge_list(slide, badge_title, badge_subtitle, items, ...)`

A circular badge on the left + stacked info bars on the right. Suitable for "core concept explanation / key conclusion / module summary / transition page".

Each item in `items` is:

```python
(title, desc, color)
```

## `layout_focus_statement(slide, statement, evidences=None, ...)`

A central large viewpoint + evidence cards below. Each item in `evidences` is:

```python
(title, desc, color)
```

## `layout_conclusion(slide, main_points, next_steps=None, ...)`

- `main_points`: a list of conclusion short sentences, at most 5 items.
- `next_steps`: a list of next-step action short sentences, at most 4 items.

## General Parameter-Passing Rules

- List fields of composite functions must be passed as lists, not strings.
- Do not arbitrarily increase or decrease tuple lengths.
- Page titles, card titles, tags, and footers must all be replaced with content from the user's topic.
- If you are unsure of the internal call relationships of a function, first open `factory/chat_features/ppt-gen/scripts/pptx_base.py` to view the definition.
