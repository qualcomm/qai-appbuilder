//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_MEMO_DOC_SCAN_H
#define TASK_MEMO_DOC_SCAN_H

#include <nlohmann/json.hpp>
#include <algorithm>
#include <cctype>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace task_memo_scan
{
using json = nlohmann::ordered_json;

constexpr size_t kMaxHeadsPerFile = 32;
constexpr size_t kMaxIdentsPerFile = 64;
constexpr size_t kHeadTitleChars = 60;
constexpr size_t kSectionTitleChars = 24;
constexpr size_t kReadFileSuffixChars = 480;
constexpr size_t kMaxProtectedRefs = 4;
constexpr size_t kShortenPathMinChars = 64;

constexpr size_t kNearCursorSnippetChars = 600;

constexpr size_t kMaxReadRangesPerFile = 12;

constexpr size_t kMaxDigestsPerFile = 8;
constexpr size_t kDigestGistChars = 48;
constexpr size_t kDigestsSuffixChars = 260;

struct Heading
{
    size_t line = 0;
    std::string title;
};

struct DocRef
{
    std::string path;
    std::string section;
    std::string cite;
    bool marked = false;
};

struct PageText
{
    std::string text;
    size_t first = 1;
};

size_t Utf8Floor(const std::string &s, size_t pos);

std::string Trim(const std::string &s);

std::string ClipHead(const std::string &s, size_t cap);

std::string ClipKeepTail(const std::string &s, size_t cap);

std::string Lower(std::string s);

std::string FirstCommandToken(const std::string &command);

bool IsIdempotentExecCommand(const std::string &command);

std::string NormalizeExecCommandForCache(const std::string &command);

std::vector<std::string> SplitLines(const std::string &text);

bool IsRuntimeLogLine(const std::string &line);

bool IsBinaryContent(const std::string &content);

std::string BinaryResultStub(size_t size);

bool IsToolNoiseLine(const std::string &trimmed);

std::string TailFacts(const std::string &content, size_t cap);

bool IsFenceLine(const std::string &line);

bool ParsePagedReadFooter(const std::string &content, size_t &first, size_t &last, size_t &total);

bool HasPagedReadFooter(const std::string &content);

PageText ExtractPageText(const std::string &content, size_t first_line_hint);

bool IsPathChar(char c);

bool IsPathSep(char c);

std::string ShortenPathToken(const std::string &tok);

std::string ShortenLongPaths(const std::string &text);

const std::vector<std::string> &HardRuleMarkers();

bool IsHardRuleLine(const std::string &line);

bool EndsWithLabelColon(const std::string &text);

bool HasRuleSubstance(const std::string &text);

std::vector<std::string> ExtractHardRules(const std::string &content, size_t max_rules, size_t cap);

void AdmitHardRule(json &rules, json &sources, const std::string &rule, const std::string &doc, size_t cap);

std::vector<Heading> ExtractHeadings(const std::string &text, size_t first_line);

std::string ExtractNearCursorSnippet(const std::string &text, size_t first_line,
                                            const std::vector<Heading> &fresh_heads, size_t &anchor_line_out);

std::string NormalizePath(const std::string &path);

bool EndsWithPath(const std::string &longer, const std::string &suffix);

bool PathMatches(const std::string &a, const std::string &b);

std::vector<DocRef> ExtractDocRefs(const std::string &text, size_t max_refs, size_t cite_cap);

bool HasScriptExtension(const std::string &lower);

std::string FirstCommandWord(const std::string &line);

std::vector<std::string> SplitNameTokens(const std::string &line);

std::vector<std::pair<std::string, size_t>> ExtractIdentifiers(const std::string &text, size_t first_line,
                                                                      size_t max_n);

int FindIdentLine(const json &idents, const std::vector<std::string> &call_tokens);

using LineRange = std::pair<size_t, size_t>;

std::vector<LineRange> LoadLineRanges(const json &arr);

std::string JoinLineRanges(const std::vector<LineRange> &ranges);

void AddReadRange(json &file_entry, size_t first, size_t last, size_t total);

bool ApplyOverlapProgressReset(json &entry, const std::string &read_key,
                                      const std::string &seen_before_joined,
                                      const std::string &seen_after_joined);

struct PageDigest
{
    size_t first = 0;
    size_t last = 0;
    std::string gist;
};

std::vector<PageDigest> LoadPageDigests(const json &file_entry);

std::string MakePageGist(const std::string &text, const std::vector<Heading> &fresh_heads,
                                const std::vector<std::pair<std::string, size_t>> &idents);

void AddPageDigest(json &file_entry, size_t first, size_t last, const std::string &gist);

void FoldPage(json &file_entry, const std::string &text, size_t first_line);

bool IsMarkedRef(const json &value);

size_t CountMarkedRefs(const json &pending);

bool EraseOldestRef(json &pending, bool marked, const std::string &keep = "");

void MakeRoomForMarkedRef(json &pending, const std::string &keep);

void FoldRefs(json &pending, const std::vector<DocRef> &refs, const json &read_files,
                     const std::string &own_path, size_t cap);

void ClearReadRef(json &pending, const std::string &read_path);

std::string LastNameOf(const std::string &path);

std::vector<Heading> LoadHeadings(const json &file_entry);

std::string RenderSectionsSuffix(const json &file_entry, size_t budget);

std::string RenderCursorSuffix(const json &file_entry);

std::string RenderDigestsSuffix(const json &file_entry, size_t budget);

std::string RenderReadFileSuffix(const json &file_entry, size_t budget);

bool IsFileFullyRead(const json &file_entry);

std::vector<LineRange> UnreadGaps(const json &file_entry);

size_t NextUnreadOffset(const json &file_entry);

std::string RenderReadFileBase(const std::string &name, const json &value);

void EvictReadFilesByValue(json &read_files, size_t cap);

std::string RenderNearCursorFact(const std::string &path, const json &file_entry);

std::string DisplayPathName(const std::string &path, bool with_parent);

std::vector<std::string> SplitCommandWords(const std::string &command);

std::string CompactCommand(const std::string &command);

std::string RenderPendingRefs(const json &pending);

bool IsWriteLikeRow(const std::string &row);

size_t CountOccurrences(const std::string &s, const char *needle);

std::string ExtractCodeExcerpt(const std::string &text, size_t cap);

void AppendCompletedRow(json &field, const std::string &row, size_t cap, const std::string &supersede_key = "");

void AppendRecoveryEpisode(json &ledger, const std::string &row, size_t cap);

std::vector<std::string> ExtractPathLikeTokens(const std::string &text);

std::string FindArtifactHit(const json &artifacts, const std::string &probe, bool probe_is_command_text);

void ClaimArtifact(json &artifacts, const std::string &path, const std::string &call_summary);

void ConfirmOrRevokeArtifact(json &artifacts, const std::string &path, bool ok);

std::string RenderArtifactLine(const std::string &path, const json &item);

std::string RenderTriedQueriesSummary(const json &tried_queries, const json &tried_queries_count);

std::string RenderPageDirectorySummary(const json &pages);

}

#endif
