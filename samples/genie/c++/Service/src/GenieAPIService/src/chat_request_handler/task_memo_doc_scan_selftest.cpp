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
            "| Routing | MUST read this |\n"
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
                     Contains(rendered, "an action was last matched to line 30 (under \"Boundary\")"));
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

    out << "=================================================================================\n";
    out << "Total cases: " << runner.total() << ", passed: " << runner.passed()
        << " (" << (runner.total() > 0 ? (100.0 * runner.passed() / runner.total()) : 100.0) << "%)\n";
    out << "=================================================================================\n";

    return runner.passed() == runner.total();
}
