//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_memo_doc_scan_selftest.h"
#include "task_memo_doc_scan.h"
#include "task_memo_builder.h"

#include <string>
#include <vector>

namespace
{

using task_memo_scan::json;

class SelfTestRunner
{
public:
    explicit SelfTestRunner(std::ostream &out) : out_(out) {}

    void Check(const std::string &id, const std::string &description, bool condition)
    {
        ++total_;
        if (condition)
        {
            ++passed_;
        }
        out_ << "[" << (condition ? "PASS" : "FAIL") << "] " << id << " -- " << description << "\n";
    }

    int total() const { return total_; }
    int passed() const { return passed_; }

private:
    std::ostream &out_;
    int total_ = 0;
    int passed_ = 0;
};

bool Contains(const std::string &haystack, const std::string &needle)
{
    return haystack.find(needle) != std::string::npos;
}

const char kSection[] = "\xC2\xA7";

}

bool RunTaskMemoDocScanSelfTest(std::ostream &out)
{
    SelfTestRunner runner(out);
    out << "==================== Task Memo Doc Scan Self-Test ====================\n";

    {
        const std::string doc_page =
            "<tool_response>\nlist empty. (Exception: `x` after a run logs ...)\nError: this line is document text\n"
            "...[read truncated: context budget; showed lines 1-22 of total 198; next offset=23]\n"
            "[snapshot: 718cab418602]\n</tool_response>\n";
        const std::string bare = "list empty. (Exception: `x` after a run logs ...)\nError: this line is document text\n";
        runner.Check("failure_paged_read_exempt", "paged read result is never a failure even with Error: lines",
                     !TaskMemoBuilder::LooksLikeToolFailure(doc_page));
        runner.Check("failure_same_text_without_footer", "same text without a paged footer is still a failure",
                     TaskMemoBuilder::LooksLikeToolFailure(bare));
        runner.Check("failure_prose_exception_midline", "Exception: in the middle of a prose line is not a signal",
                     !TaskMemoBuilder::LooksLikeToolFailure("list empty. (Exception: `x` after a run)\n"));
        runner.Check("failure_exit_code_nonzero", "non-zero exit marker is a failure",
                     TaskMemoBuilder::LooksLikeToolFailure("---\n[exit code: 1]"));
        runner.Check("failure_exit_code_zero", "zero exit marker is not a failure",
                     !TaskMemoBuilder::LooksLikeToolFailure("done\n[exit code: 0]\n"));
        runner.Check("failure_first_line_signal", "FirstFailureLine returns the trimmed signal line",
                     TaskMemoBuilder::FirstFailureLine("ok\n  Error: boom 42\n") == "Error: boom 42");
        runner.Check("failure_first_line_exit_only", "FirstFailureLine falls back to nonzero_exit",
                     TaskMemoBuilder::FirstFailureLine("---\n[exit code: 2]") == "nonzero_exit");
        runner.Check("failure_first_line_clean", "FirstFailureLine is empty for a clean result",
                     TaskMemoBuilder::FirstFailureLine("all good\n").empty());
    }

    {
        const std::string content =
            "<tool_response>\nline-a\nline-b\n"
            "...[read truncated: context budget; showed lines 23-24 of total 198; next offset=25]\n"
            "[snapshot: abc]\n</tool_response>\n";
        const auto page = task_memo_scan::ExtractPageText(content, 1);
        runner.Check("page_text_first", "first line comes from the footer", page.first == 23);
        runner.Check("page_text_body", "page text excludes wrapper, footer and snapshot", page.text == "line-a\nline-b");
        const auto plain = task_memo_scan::ExtractPageText("x1\nx2\n[snapshot: q]\n", 40);
        runner.Check("page_text_no_footer", "no footer: hint is used and snapshot line is cut",
                     plain.first == 40 && plain.text == "x1\nx2");
    }

    {
        const std::string text = "## Step 4 Convert\nbody\n```bash\n# not a heading\n```\n### Sub ###\n#nospace";
        const auto heads = task_memo_scan::ExtractHeadings(text, 55);
        runner.Check("headings_count", "fenced and malformed headings are ignored", heads.size() == 2);
        runner.Check("headings_lines", "absolute line numbers follow the first line",
                     heads.size() == 2 && heads[0].line == 55 && heads[1].line == 60);
        runner.Check("headings_titles", "titles are trimmed of markers",
                     heads.size() == 2 && heads[0].title == "Step 4 Convert" && heads[1].title == "Sub");
    }

    {
        const std::string content =
            "## Boundary Decision (MANDATORY GATE)\n"
            "| Routing | read this |\n"
            "- You MUST run the init step first -> `references/operations_reference.md " + std::string(kSection) +
            " Working Directory`\n"
            "> **NEVER** delete the plan file\n";
        const auto rules = task_memo_scan::ExtractHardRules(content, 6, 220);
        runner.Check("rules_skip_heading_and_table", "heading and table rows are not rules", rules.size() == 2);
        runner.Check("rules_keep_pointer", "pointer at the end of a rule survives",
                     !rules.empty() && Contains(rules[0], "Working Directory`"));
        const std::string long_line = std::string(300, 'x') + " MUST do it -> tail.md";
        const auto clipped = task_memo_scan::ExtractHardRules(long_line, 6, 100);
        runner.Check("rules_clip_keeps_tail", "clipping keeps the end of the line",
                     clipped.size() == 1 && clipped[0].size() <= 100 &&
                         clipped[0].compare(clipped[0].size() - 7, 7, "tail.md") == 0);
    }

    {
        const std::string text =
            "> 7. Bootstrap with `qai_workspace_init.py` -> `references/operations_reference.md " + std::string(kSection) +
            " Working Directory`.\n"
            "| Error | see `references/other.md` |\n"
            "Read `C:\\Users\\x\\proj/factory/_shared/x64-host-notes.md` FIRST\n"
            "see:references/foo.md and SKILL.md and https://a.b/c.md\n"
            "```\nreferences/in_fence.md\n```\n";
        const auto refs = task_memo_scan::ExtractDocRefs(text, 8, 160);
        runner.Check("refs_count", "table, fenced, bare-name and url references are ignored", refs.size() == 3);
        runner.Check("refs_section", "path and section are split on the section sign",
                     refs.size() > 0 && refs[0].path == "references/operations_reference.md" &&
                         refs[0].section == "Working Directory");
        runner.Check("refs_cite_has_context", "cite keeps the sentence", refs.size() > 0 && Contains(refs[0].cite, "Bootstrap with"));
        runner.Check("refs_absolute_path", "absolute path keeps its drive and uses forward slashes",
                     refs.size() > 1 && refs[1].path.compare(0, 11, "C:/Users/x/") == 0 &&
                         Contains(refs[1].path, "x64-host-notes.md"));
        runner.Check("refs_colon_prefix", "prefix before a colon is cut off",
                     refs.size() > 2 && refs[2].path == "references/foo.md");
    }

    {
        const std::string text =
            "Run `python scripts/Run_Pipeline.py --x` then\n```bash\nqairt-converter --input_network m.onnx\ncd work\n```\n";
        const auto ids = task_memo_scan::ExtractIdentifiers(text, 10, 64);
        json idents = json::object();
        for (const auto &id : ids)
            idents[id.first] = id.second;
        runner.Check("idents_script_ext", "script basenames are lowercased with their absolute line",
                     idents.contains("run_pipeline.py") && idents["run_pipeline.py"] == 10);
        runner.Check("idents_fence_command", "first token of a fenced command line is an identifier",
                     idents.contains("qairt-converter") && idents["qairt-converter"] == 12);
        runner.Check("idents_skip_builtins", "short shell words and data files are not identifiers",
                     !idents.contains("cd") && !idents.contains("work") && !idents.contains("python") &&
                         !idents.contains("m.onnx"));
        const auto tokens = task_memo_scan::SplitNameTokens(
            "{\"name\":\"exec\",\"arguments\":{\"command\":\"python C:\\\\w\\\\Run_Pipeline.py --x\"}}");
        runner.Check("idents_match_call", "a call that names the script is matched back to its line",
                     task_memo_scan::FindIdentLine(idents, tokens) == 10);
        const auto other = task_memo_scan::SplitNameTokens("{\"name\":\"exec\",\"arguments\":{\"command\":\"dir\"}}");
        runner.Check("idents_no_match", "an unrelated call has no source", task_memo_scan::FindIdentLine(idents, other) == 0);
    }

    {
        json file_entry = json::object();
        task_memo_scan::FoldPage(file_entry, "# Title\nx\n## Second\ny", 1);
        task_memo_scan::FoldPage(file_entry, "## Second\ny\n## Third", 3);
        task_memo_scan::FoldPage(file_entry, "## Second\ny\n## Third", 3);
        const auto heads = task_memo_scan::LoadHeadings(file_entry);
        runner.Check("fold_heads_merge", "headings from several pages merge by absolute line without duplicates",
                     heads.size() == 3 && heads[0].line == 1 && heads[1].line == 3 && heads[2].line == 5);
    }

    {
        json pending = json::object();
        json read_files = json::object();
        read_files["C:/m/references/already.md"] = json::object();
        std::vector<task_memo_scan::DocRef> refs = {
            {"references/operations_reference.md", "Working Directory", "cite-a"},
            {"references/already.md", "", "cite-b"},
            {"SKILL.md/self.md", "", "cite-c"},
        };
        task_memo_scan::FoldRefs(pending, refs, read_files, "C:/m/SKILL.md/self.md", 8);
        runner.Check("refs_fold_filters", "already-read and self references are not pending",
                     pending.size() == 1 && pending.contains("references/operations_reference.md"));
        task_memo_scan::ClearReadRef(pending, "C:\\Users\\x\\references\\operations_reference.md");
        runner.Check("refs_clear_on_read", "reading the document clears its pending reference", pending.empty());
        json capped = json::object();
        std::vector<task_memo_scan::DocRef> many;
        for (int i = 0; i < 12; ++i)
            many.push_back({"d/doc" + std::to_string(i) + ".md", "", "c"});
        task_memo_scan::FoldRefs(capped, many, json::object(), "", 8);
        runner.Check("refs_fold_cap", "pending references are capped and keep the newest",
                     capped.size() == 8 && capped.contains("d/doc11.md") && !capped.contains("d/doc0.md"));
    }

    {
        json rows = json::array();
        task_memo_scan::AppendCompletedRow(rows, "ok: write plan", 3);
        task_memo_scan::AppendCompletedRow(rows, "ok: exec a", 3);
        task_memo_scan::AppendCompletedRow(rows, "ok: exec b", 3);
        task_memo_scan::AppendCompletedRow(rows, "ok: exec c", 3);
        runner.Check("completed_evicts_non_write", "oldest non-write row is evicted first",
                     rows.size() == 3 && rows[0] == "ok: write plan" && rows[1] == "ok: exec b" && rows[2] == "ok: exec c");
        task_memo_scan::AppendCompletedRow(rows, "ok: exec c", 3);
        runner.Check("completed_dedup", "identical rows are not appended twice", rows.size() == 3);
        task_memo_scan::AppendCompletedRow(rows, "ok: write y", 3);
        task_memo_scan::AppendCompletedRow(rows, "ok: write z", 3);
        runner.Check("completed_keeps_older_write", "newer write rows never evict an older write while a non-write row exists",
                     rows.size() == 3 && rows[0] == "ok: write plan" && rows[1] == "ok: write y" && rows[2] == "ok: write z");
        task_memo_scan::AppendCompletedRow(rows, "ok: write w", 3);
        runner.Check("completed_all_write_fallback", "when only write rows remain the oldest goes",
                     rows.size() == 3 && rows[0] == "ok: write y" && rows[2] == "ok: write w");
    }

    {
        json entry = json::object();
        entry["original_goal_raw"] = "goal text";
        entry["read_files"]["C:/m/SKILL.md"]["seen"] = json::array({json::array({1, 198})});
        entry["read_files"]["C:/m/SKILL.md"]["total"] = 198;
        entry["read_files"]["C:/m/SKILL.md"]["heads"] =
            json::array({json::array({5, "How to use"}), json::array({25, "Boundary"})});
        entry["read_files"]["C:/m/SKILL.md"]["used_line"] = 30;
        entry["pending_refs"]["references/operations_reference.md"]["section"] = "Working Directory";
        entry["pending_refs"]["references/operations_reference.md"]["cite"] = "Bootstrap with qai_workspace_init.py";
        const std::string rendered = TaskMemoBuilder::Render(entry, 0);
        runner.Check("render_sections", "read line lists the headings seen",
                     Contains(rendered, "sections seen: L5 How to use; L25 Boundary"));
        runner.Check("render_cursor", "read line names the heading the last action was matched under",
                     Contains(rendered, "last action matched L30 (under \"Boundary\")"));
        runner.Check("render_pending_refs", "pending references block carries path, section and cite",
                     Contains(rendered, "references/operations_reference.md " + std::string(kSection) + " Working Directory") &&
                         Contains(rendered, "Bootstrap with qai_workspace_init.py"));
        runner.Check("render_no_nudge", "read block states facts without telling the model to continue reading",
                     !Contains(rendered, "Continue with") && !Contains(rendered, "re-read a range"));
    }

    {
        json entry = json::object();
        entry["original_goal_raw"] = "goal";
        json rows = json::array();
        rows.push_back("ok: write {\"path\":\"plan.md\"} -> saved");
        for (int i = 1; i <= 12; ++i)
            rows.push_back("ok: exec {\"command\":\"step " + std::to_string(i) + "\"} -> " + std::string(40, 'r'));
        entry["completed"] = rows;
        const std::string rendered = TaskMemoBuilder::Render(entry, 480);
        runner.Check("render_completed_degrades", "completed block degrades by rows instead of vanishing",
                     Contains(rendered, "rows omitted to fit") && rendered.size() <= 480);
        runner.Check("render_completed_keeps_write", "write rows survive the degrade", Contains(rendered, "plan.md"));
        runner.Check("render_completed_keeps_newest", "newest rows survive the degrade", Contains(rendered, "step 12"));
        runner.Check("render_completed_drops_old", "oldest non-write rows are the ones dropped", !Contains(rendered, "step 1\""));
    }

    {
        const std::string success =
            "Loading model\n<E> Error 0x200: failed to close queue\n[Results] Detected 3 objects\n"
            "[DONE] Annotated image saved: tabletop_det.jpg\n";
        runner.Check("failure_runtime_log_ignored", "runtime shutdown diagnostics do not turn a successful result into a failure",
                     !TaskMemoBuilder::LooksLikeToolFailure(success));
        runner.Check("failure_runtime_error_retained", "a runtime log with a real failure signal remains visible to failure extraction",
                     Contains(TaskMemoBuilder::FirstFailureLine("<E> Failed to load skel\n[exit code: 1]"), "Failed to load skel"));
        runner.Check("tail_facts_drop_noise", "success facts keep result lines and drop runtime, warning and timing noise",
                     task_memo_scan::TailFacts("Loading\n<E> Error 0x200: failed to close queue\n[WARNING] noisy\n12ms [done]\n"
                                                "[Results] Detected 3 objects\n[DONE] Saved tabletop_det.jpg\n", 500) ==
                         "Loading\n[Results] Detected 3 objects\n[DONE] Saved tabletop_det.jpg");
    }

    {
        const std::string binary = std::string(300, 'A') + std::string("\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD", 30);
        const std::string markdown = std::string(300, 'a') + "\ntext\n";
        runner.Check("binary_content_detected", "binary-like replacement content is detected",
                     task_memo_scan::IsBinaryContent(binary));
        runner.Check("binary_content_stub", "binary content is represented by a compact deterministic stub",
                     task_memo_scan::BinaryResultStub(binary.size()).find("not displayed") != std::string::npos);
        runner.Check("binary_markdown_not_detected", "plain text is not classified as binary",
                     !task_memo_scan::IsBinaryContent(markdown));
        runner.Check("binary_short_not_detected", "short binary-like content is left alone",
                     !task_memo_scan::IsBinaryContent(std::string(200, '\0')));
    }

    {
        const std::string text =
            "# Title with MUST in heading\n"
            "```python\n"
            "# --- QNN config (MUST be before QNNContext) ---\n"
            "x = 1\n"
            "```\n"
            "| QNNConfig.Config | MUST be called once |\n"
            "Every inference -> read htp.md first. Non-negotiable: do it.\n";
        const auto rules = task_memo_scan::ExtractHardRules(text, 6, 220);
        runner.Check("rules_fence_comment", "a marker comment inside a code fence is a rule and a heading outside is not",
                     rules.size() == 3 && Contains(rules[0], "QNN config (MUST be before QNNContext)"));
        runner.Check("rules_table_with_marker", "a table row that carries a marker is a rule",
                     rules.size() == 3 && Contains(rules[1], "QNNConfig.Config"));
        runner.Check("rules_nonnegotiable_marker", "Non-negotiable is a marker",
                     rules.size() == 3 && Contains(rules[2], "Non-negotiable"));
    }

    {
        json rules = json::array();
        json sources = json::array();
        for (const char *r : {"r1", "r2", "r3"})
            task_memo_scan::AdmitHardRule(rules, sources, r, "a.md", 3);
        task_memo_scan::AdmitHardRule(rules, sources, "b1", "b.md", 3);
        runner.Check("rules_admit_new_doc", "a new document evicts the newest rule of the dominant document",
                     rules.size() == 3 && rules[0] == "r1" && rules[1] == "r2" && rules[2] == "b1");
        task_memo_scan::AdmitHardRule(rules, sources, "b2", "b.md", 3);
        runner.Check("rules_admit_balanced", "a document is not admitted past an even share",
                     rules.size() == 3 && rules[2] == "b1");
        task_memo_scan::AdmitHardRule(rules, sources, "c1", "c.md", 3);
        runner.Check("rules_admit_keeps_floor", "every document keeps at least one rule",
                     rules.size() == 3 && rules[0] == "r1" && rules[1] == "b1" && rules[2] == "c1");
        task_memo_scan::AdmitHardRule(rules, sources, "c1", "d.md", 3);
        runner.Check("rules_admit_dedup", "an identical rule is not admitted twice", rules.size() == 3 && sources.size() == 3);
    }

    {
        const std::string text = "Read `references/x.md` for details\nEvery run MUST read `references/x.md` first\n";
        const auto refs = task_memo_scan::ExtractDocRefs(text, 4, 160);
        runner.Check("refs_marked_cite_wins", "a later mention that carries a marker replaces the plain citation",
                     refs.size() == 1 && refs[0].marked && Contains(refs[0].cite, "MUST"));
        json pending = json::object();
        std::vector<task_memo_scan::DocRef> plain = {{"d/a.md", "", "plain", false}};
        std::vector<task_memo_scan::DocRef> marked = {{"d/a.md", "", "MUST do", true}};
        task_memo_scan::FoldRefs(pending, plain, json::object(), "", 8);
        task_memo_scan::FoldRefs(pending, marked, json::object(), "", 8);
        runner.Check("refs_fold_marked_replaces", "folding a marked mention replaces the pending citation",
                     pending.size() == 1 && pending["d/a.md"]["cite"] == "MUST do");
    }

    {
        const std::string script =
            "\"\"\"QNN NPU inference.\nProcess B.\n\"\"\"\nimport sys, os\n"
            "from qai_appbuilder import QNNContext, QNNConfig\n"
            "# --- QNN config (MUST be before QNNContext) ---\nQNNConfig.Config(\n    Runtime.HTP\n)\n"
            "# plain comment\nclass A:\n    pass\n";
        const std::string excerpt = task_memo_scan::ExtractCodeExcerpt(script, 300);
        runner.Check("excerpt_skips_docstring", "the module docstring is not part of the excerpt", !Contains(excerpt, "Process B"));
        runner.Check("excerpt_keeps_imports_and_call", "imports and the configuration call are kept",
                     Contains(excerpt, "import sys, os") && Contains(excerpt, "QNNConfig.Config("));
        runner.Check("excerpt_keeps_marker_comment", "a marker comment is kept and a plain comment is not",
                     Contains(excerpt, "notes: QNN config (MUST be before QNNContext)") && !Contains(excerpt, "plain comment"));
        json rows = json::array();
        task_memo_scan::AppendCompletedRow(rows, "ok: write p.py :: v1 -> saved", 5, "write p.py :: ");
        task_memo_scan::AppendCompletedRow(rows, "ok: write q.py :: v1 -> saved", 5, "write q.py :: ");
        task_memo_scan::AppendCompletedRow(rows, "ok: write p.py :: v2 -> saved", 5, "write p.py :: ");
        runner.Check("completed_supersede", "a newer write of the same file replaces the older row only",
                     rows.size() == 2 && rows[0] == "ok: write q.py :: v1 -> saved" && rows[1] == "ok: write p.py :: v2 -> saved");
    }

    {
        runner.Check("failure_signature_python_exception", "the exception line is the signature, not an earlier noise line",
                     TaskMemoBuilder::FirstFailureLine(
                         "Loading x\nWarmup run...\n<E> Error 0x200: failed to close queue\nTraceback (most recent call last):\n"
                         "  File \"a.py\", line 1\nTypeError: Inference(): incompatible function arguments.\n[exit code: 1]") ==
                         "TypeError: Inference(): incompatible function arguments.");
        runner.Check("failure_signature_exit_tail", "an exit-only failure is keyed by its last output line",
                     TaskMemoBuilder::FirstFailureLine("dir listing\nFile Not Found\n---\n[exit code: 1]") ==
                         "exit: File Not Found");
        const std::string call =
            "<tool_call>{\"name\":\"read\",\"arguments\":{\"path\":\"C:\\\\a\\\\b.md\",\"offset\":5}}</tool_call>";
        runner.Check("read_path_json_decoded", "a JSON-escaped path is decoded exactly once",
                     TaskMemoBuilder::ExtractReadToolPath(call) == "C:\\a\\b.md#range:offset=5");
    }

    {
        json many_heads = json::object();
        many_heads["heads"] = json::array();
        for (int i = 0; i < 14; ++i)
            many_heads["heads"].push_back(json::array({10 + i * 20, "Section number " + std::to_string(i) + " with a very long trailing title"}));
        const std::string full = task_memo_scan::RenderSectionsSuffix(many_heads, 2000);
        runner.Check("sections_titles_clipped", "long titles are clipped so every heading fits",
                     Contains(full, "L10 ") && Contains(full, "L270 ") && !Contains(full, "trailing title"));
        const std::string tight = task_memo_scan::RenderSectionsSuffix(many_heads, 200);
        runner.Check("sections_drop_earliest", "a tight budget drops the earliest headings and keeps the latest",
                     tight.size() <= 200 && Contains(tight, "sections seen: ... ") && Contains(tight, "L270 ") &&
                         !Contains(tight, "L10 "));
        runner.Check("sections_too_small", "a budget below one heading renders nothing",
                     task_memo_scan::RenderSectionsSuffix(many_heads, 20).empty());
    }

    {
        json entry = json::object();
        entry["original_goal_raw"] = "goal text";
        entry["memo_reserve_ratio"] = 0.3;
        json rules = json::array();
        for (int i = 0; i < 12; ++i)
            rules.push_back("rule " + std::to_string(i) + " " + std::string(210, 'r'));
        entry["hard_rules"] = rules;
        json done = json::array();
        for (int i = 0; i < 20; ++i)
            done.push_back("ok: exec step " + std::to_string(i) + " " + std::string(180, 'c'));
        entry["completed"] = done;
        for (int f = 0; f < 3; ++f)
        {
            const std::string key = "C:/m/doc" + std::to_string(f) + ".md";
            entry["read_files"][key]["seen"] = json::array({json::array({1, 120}), json::array({200, 300})});
            entry["read_files"][key]["total"] = 355;
            json heads = json::array();
            for (int i = 0; i < 14; ++i)
                heads.push_back(json::array({10 + i * 25, "Step " + std::to_string(i) + " heading"}));
            entry["read_files"][key]["heads"] = heads;
            entry["read_files"][key]["used_line"] = 130;
        }
        const std::string rendered = TaskMemoBuilder::Render(entry, 9778);
        runner.Check("read_block_survives_full_memo", "the read index is rendered even when other blocks fill the budget",
                     Contains(rendered, "Files already read") && rendered.size() <= 9778);
        runner.Check("read_block_keeps_late_headings", "the latest headings of every file stay visible",
                     Contains(rendered, "L335 Step 13 heading"));
        const size_t block_begin = rendered.find("Files already read");
        runner.Check("read_block_capped", "the read block stays within its own share of the budget",
                     block_begin != std::string::npos && rendered.size() - block_begin <= 9778 * 9 / 40 + 8);
    }

    {
        json entry = json::object();
        entry["original_goal_raw"] = "goal";
        for (int f = 0; f < 40; ++f)
        {
            const std::string key = "C:/d/f" + std::string(f < 10 ? "0" : "") + std::to_string(f) + ".md";
            entry["read_files"][key]["seen"] =
                json::array({json::array({1, 5}), json::array({10, 15}), json::array({20, 25})});
            entry["read_files"][key]["total"] = 100;
        }
        const std::string rendered = TaskMemoBuilder::Render(entry, 0);
        runner.Check("read_block_drops_oldest_files", "too many files drop the oldest first and say so",
                     Contains(rendered, "earlier file(s) omitted to fit") && Contains(rendered, "- f39.md") &&
                         !Contains(rendered, "- f00.md"));
        json same = json::object();
        same["original_goal_raw"] = "goal";
        same["read_files"]["C:/a/SKILL.md"]["seen"] = json::array({json::array({1, 10})});
        same["read_files"]["C:/b/SKILL.md"]["seen"] = json::array({json::array({1, 10})});
        const std::string named = TaskMemoBuilder::Render(same, 0);
        runner.Check("read_block_name_collision", "files with the same basename are told apart by their parent directory",
                     Contains(named, "- a/SKILL.md") && Contains(named, "- b/SKILL.md"));
    }

    {
        json pending = json::object();
        std::vector<task_memo_scan::DocRef> first = {{"d/must.md", "", "MUST read", true}};
        task_memo_scan::FoldRefs(pending, first, json::object(), "", 8);
        std::vector<task_memo_scan::DocRef> plain;
        for (int i = 0; i < 10; ++i)
            plain.push_back({"d/p" + std::to_string(i) + ".md", "", "c", false});
        task_memo_scan::FoldRefs(pending, plain, json::object(), "", 8);
        runner.Check("refs_marked_survives_plain", "plain references never push a marked one out",
                     pending.size() == 8 && pending.contains("d/must.md") && !pending.contains("d/p0.md") &&
                         pending.contains("d/p9.md"));
        json many_marked = json::object();
        std::vector<task_memo_scan::DocRef> marked;
        for (int i = 0; i < 5; ++i)
            marked.push_back({"d/m" + std::to_string(i) + ".md", "", "MUST", true});
        task_memo_scan::FoldRefs(many_marked, marked, json::object(), "", 8);
        runner.Check("refs_marked_protection_cap", "protection is capped: the oldest marked reference leaves first",
                     many_marked.size() == 4 && !many_marked.contains("d/m0.md") && many_marked.contains("d/m4.md"));
        json upgrade = json::object();
        std::vector<task_memo_scan::DocRef> base;
        for (int i = 0; i < 4; ++i)
            base.push_back({"d/n" + std::to_string(i) + ".md", "", "MUST", true});
        base.push_back({"d/u.md", "", "plain", false});
        task_memo_scan::FoldRefs(upgrade, base, json::object(), "", 8);
        std::vector<task_memo_scan::DocRef> promote = {{"d/u.md", "", "MUST now", true}};
        task_memo_scan::FoldRefs(upgrade, promote, json::object(), "", 8);
        runner.Check("refs_upgrade_respects_cap", "promoting a plain reference also respects the protection cap",
                     upgrade.contains("d/u.md") && upgrade["d/u.md"]["cite"] == "MUST now" &&
                         !upgrade.contains("d/n0.md") && task_memo_scan::CountMarkedRefs(upgrade) == 4);
    }

    {
        json rows = json::array();
        task_memo_scan::AppendCompletedRow(rows, "成功: exec python.exe infer.py --model x.bin -> done", 3,
                                           "exec python.exe infer.py");
        for (int i = 0; i < 20; ++i)
            task_memo_scan::AppendCompletedRow(rows, "成功: exec dir /b item" + std::to_string(i), 3);
        runner.Check("completed_protects_script_exec", "script exec rows survive observation-only exec rows",
                     rows.size() == 3 && Contains(rows[0].get<std::string>(), "infer.py"));
        task_memo_scan::AppendCompletedRow(rows, "成功: exec python.exe infer.py --model y.bin -> done", 3,
                                           "exec python.exe infer.py");
        runner.Check("completed_supersedes_script_exec", "a newer compact script exec replaces the older row",
                     rows.size() == 3 && Contains(rows[2].get<std::string>(), "--model y.bin"));
    }

    {
        const std::string text =
            "Every inference MUST read `C:\\Users\\HCKTest\\Desktop\\ok\\QAIModelBuilder-main\\QAIModelBuilder-main\\"
            "factory\\chat_features\\_shared\\htp-inference-performance.md` first\n";
        const auto rules = task_memo_scan::ExtractHardRules(text, 6, 200);
        runner.Check("rules_long_path_keeps_filename", "a long absolute path shrinks to its last two components",
                     rules.size() == 1 && Contains(rules[0], "...\\_shared\\htp-inference-performance.md") &&
                         !Contains(rules[0], "HCKTest"));
        const auto rel = task_memo_scan::ExtractHardRules(
            "You MUST open `references/model-architectures/multi_component_pipeline.md` first\n", 6, 200);
        runner.Check("rules_short_relative_path_kept", "a normal relative pointer is left untouched",
                     rel.size() == 1 && Contains(rel[0], "references/model-architectures/multi_component_pipeline.md"));
        const auto merged = task_memo_scan::ExtractHardRules(
            "**Pre-quantization checklist (MANDATORY):**\n- run the calibration list check first\n", 6, 220);
        runner.Check("rules_label_merges_body", "a label line takes the first body line after it",
                     merged.size() == 1 && Contains(merged[0], "Pre-quantization checklist") &&
                         Contains(merged[0], "calibration list"));
        const auto empty_label = task_memo_scan::ExtractHardRules("\xE7\xA6\x81\xE6\xAD\xA2:\n\n## Next\n", 6, 220);
        runner.Check("rules_empty_label_dropped", "a bare marker label with no body is dropped", empty_label.empty());
        const auto named_label = task_memo_scan::ExtractHardRules("NEVER qnn-net-run:\n\n## Next\n", 6, 220);
        runner.Check("rules_named_label_kept", "a label that still names something is kept",
                     named_label.size() == 1 && Contains(named_label[0], "qnn-net-run"));
    }

    {
        runner.Check("command_compact_venv", "interpreter and script keep their basenames, arguments are kept",
                     task_memo_scan::CompactCommand(
                         "C:\\venv\\Scripts\\python.exe C:\\long\\path\\infer_generic.py --model x.bin --precision w8a8") ==
                         "python.exe infer_generic.py --model x.bin --precision w8a8");
        runner.Check("command_compact_cmd_wrapper", "the interpreter is the last non-option word before the script",
                     task_memo_scan::CompactCommand("cmd /c python run.py -a") == "python run.py -a");
        runner.Check("command_compact_no_script", "a command without a script is left alone",
                     task_memo_scan::CompactCommand("qairt-converter --input_network m.onnx").empty() &&
                         task_memo_scan::CompactCommand("dir /s /b *labels*").empty());
        const std::string long_args = task_memo_scan::CompactCommand("python s.py " + std::string(200, 'a'));
        runner.Check("command_compact_args_clipped", "trailing arguments are clipped", long_args.size() < 90);
    }

    {
        json ledger = json::array();
        task_memo_scan::AppendRecoveryEpisode(ledger, "未解决：同类尝试 2 次，全部失败：a", 3);
        task_memo_scan::AppendRecoveryEpisode(ledger, "已解决 (第 3 次尝试): b", 3);
        task_memo_scan::AppendRecoveryEpisode(ledger, "未解决：同类尝试 1 次，全部失败：c", 3);
        task_memo_scan::AppendRecoveryEpisode(ledger, "未解决：同类尝试 4 次，全部失败：d", 3);
        runner.Check("recovery_ledger_protects_resolved", "resolved episodes survive unresolved rows at capacity",
                     ledger.size() == 3 && Contains(ledger[0].get<std::string>(), "已解决") &&
                         !Contains(ledger[0].get<std::string>(), "a"));
        const size_t before = ledger.size();
        task_memo_scan::AppendRecoveryEpisode(ledger, "已解决 (第 3 次尝试): b", 3);
        runner.Check("recovery_ledger_deduplicates", "identical recovery rows do not grow the ledger",
                     ledger.size() == before);
    }

    out << "=================================================================================\n";
    out << "Total cases: " << runner.total() << ", passed: " << runner.passed()
        << " (" << (runner.total() > 0 ? (100.0 * runner.passed() / runner.total()) : 100.0) << "%)\n";
    out << "=================================================================================\n";

    return runner.passed() == runner.total();
}
