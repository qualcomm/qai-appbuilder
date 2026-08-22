# Structured Analysis Summary of the Reference PPT

This file records the reusable design patterns abstracted from the reference PPT. It is used only to guide general PPT generation and does not reuse the reference deck's specific topic, figures, events, chapter names, or narrative content.

## 1. File Structure Observations

- Page count: 18 pages.
- Masters/layouts: includes 2 masters and 22 layout definitions, with a highly unified overall style.
- Media: a small amount of image material; the main pages are primarily composed of editable shapes, lines, and text boxes.
- Per-page element density: content pages typically contain 68–108 editable shapes and roughly 51–86 lines, indicating that page completeness comes mainly from layered native elements rather than full-page images.

## 2. Color Statistics Abstraction

High-frequency colors are concentrated in dark brown-black, gilded, cream-gold, vermilion, and teal categories:

- Gilded: close to `#D4A017` / `#D6A616`, used for titles, KPIs, nodes, and accent lines.
- Cream-gold text: close to `#F0E4C8` / `#C4AD8A`, used for body text and auxiliary descriptions.
- Vermilion: close to `#C13B2A`, used for section tags, risks/turning points/key problems.
- Teal: close to `#3D7A5F`, used for opportunities, integration, and positive results.
- Dark base and cards: close to `#1A0E0A` / `#2D1A0F` / `#4A3020`.

## 3. Font and Hierarchy Observations

- High-frequency fonts include `Microsoft YaHei`, `KaiTi`, `Segoe UI`, `Arial`, `Times New Roman`.
- Visual pattern: titles have a calligraphic or elegant feel, body text uses a clear sans-serif; English/numbers use stable Western fonts.
- Titles are usually judgment-style or pictorial expressions; subtitles use phrase groups to supplement the scope and methodology.

## 4. Page Structure Patterns

- Cover/ending pages: large centered title, top-bottom/left-right thin lines, corner marks at the four corners, page number and dotted decoration at the bottom, with a high whitespace ratio.
- Table of contents page: 2×2 large cards, with short tags, module names, descriptions, sub-pages, and page-number ranges inside the cards.
- Content pages: unified header tag, title bar, top thin line, bottom footer line, and page number.
- High information density pages: control reading order through three-column cards, 2×2 KPIs, left-right columns, timelines, and matrix-style lists.
- Transition/summary pages: use large keywords, watermarks, central conclusions, or five-point summaries to form rhythmic variation.

## 5. Page-by-Page Visual Observations of the Image Decks

- Pages 1 and 18: the cover/ending pages use full-bleed dark-toned images, dark overlays, corner marks at the four corners, a large centered title, top-bottom thin lines, and a bottom page number, with a strong echo between the first and last pages.
- Page 2: the table of contents page is a 2×2 large-card layout, with cards containing short tags, module titles, sub-page lists, and page-number ranges, with thin and uniform borders.
- Page 3: a horizontal timeline occupies the upper half of the page, and 4 KPI cards occupy the lower half, forming a "sequence + data anchor".
- Pages 4, 14, 17: a circular badge/core word on the left and stacked horizontal info bars on the right, suitable for in-depth explanation, transition pages, and summary pages.
- Pages 5, 6, 10, 15: three vertical columns of cards or multi-column cards, with different accent colors on top, and icon circles, metrics, short descriptions, and bottom tags inside.
- Pages 7, 8, 11, 16: high-density information pages organize complex content through a left-side list, a central large metric/illustration, and a right-side description area or bottom matrix.
- Pages 9, 12, 13: image-text/map/path pages use the image or background as the ambiance layer, with dark cards carrying the information, visually rich but with text still clearly layered.

## 6. Actionable Conversion

The above has been converted into the following rules and capabilities:

- `style_system.md`: responsible for visual language, color scheme, fonts, whitespace, decoration, images, and consistency rules.
- `layout_playbook.md`: responsible for page types, applicable scenarios, content generation rules, and layout selection.
- `function_contracts.md`: responsible for key function parameter contracts, avoiding field misalignment in complex layouts.
- `scripts/pptx_base.py`: provides editable layout functions for dark gilded backgrounds, cover, table of contents, sections, cards, data, workflow, timeline, comparison, case study, summary, and more.
