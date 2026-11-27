// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause

// Route A: use the host's Vue instance; no bare imports or template compiler.
export function activate(api) {
  const { h, ref, computed, onMounted } = api.vue;
  const isEnglish = computed(() =>
    String(api.locale || "zh-CN").startsWith("en"),
  );
  const tr = (zh, en) => (isEnglish.value ? en : zh);
  const list = (value) => (Array.isArray(value) ? value : []);
  const text = (value) => (value == null ? "—" : String(value));
  const message = (error) =>
    error instanceof Error ? error.message : String(error);
  const box = {
    border: "1px solid var(--el-border-color, #ccc)",
    borderRadius: "8px",
    padding: "12px",
    marginBottom: "12px",
  };
  const row = {
    display: "flex",
    flexWrap: "wrap",
    gap: "8px",
    alignItems: "center",
  };
  const preStyle = {
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    maxHeight: "360px",
    overflow: "auto",
    margin: "8px 0",
  };

  api.panel({
    title: { en: "Settings", "zh-CN": "设置", "zh-TW": "設定" },
    component: {
      name: "QualcommSkillsPanel",
      setup() {
        const packages = ref([]);
        const staged = ref(null);
        const allowNetwork = ref(false);
        const busy = ref("");
        const error = ref("");
        const mcpEnabled = ref(null);
        const mcpError = ref("");
        const readme = ref(null);
        const deleteId = ref(null);
        const loaded = ref(false);

        async function run(label, action) {
          if (busy.value) return;
          busy.value = label;
          error.value = "";
          try {
            await action();
          } catch (err) {
            error.value = message(err);
            api.toast.error(error.value);
          } finally {
            busy.value = "";
          }
        }

        async function refreshMcp() {
          try {
            const status = await api.mcpStatus();
            mcpEnabled.value =
              typeof status?.enabled === "boolean" ? status.enabled : null;
            mcpError.value =
              mcpEnabled.value === null
                ? tr(
                    "无法确认 MCP 总开关状态。",
                    "Unable to determine the global MCP status.",
                  )
                : "";
          } catch (err) {
            mcpEnabled.value = null;
            mcpError.value = `${tr("读取 MCP 状态失败：", "Failed to read MCP status: ")}${message(err)}`;
          }
        }

        async function refreshPackages() {
          const response = await api.fetch("packages");
          packages.value = list(response?.packages);
          loaded.value = true;
        }

        async function refresh() {
          await run(tr("刷新", "Refresh"), async () => {
            await Promise.all([refreshPackages(), refreshMcp()]);
          });
        }

        async function upload(event) {
          const file = event.target.files?.[0];
          event.target.value = ""; // Choosing the same ZIP again must trigger change.
          if (!file || busy.value) return;
          staged.value = null;
          allowNetwork.value = false;
          if (!/\.zip$/i.test(file.name) || file.size === 0) {
            error.value = tr(
              "请选择非空 ZIP 文件。",
              "Please select a non-empty ZIP file.",
            );
            api.toast.error(error.value);
            return;
          }
          await run(tr("上传并解析", "Upload and inspect"), async () => {
            const response = await api.upload(
              `packages/stage?filename=${encodeURIComponent(file.name)}`,
              file,
            );
            if (!response?.preview || !response?.token)
              throw new Error(
                tr(
                  "服务端未返回有效预览，请重新上传。",
                  "The server returned no valid preview. Please upload again.",
                ),
              );
            staged.value = { preview: response.preview, token: response.token };
          });
        }

        async function install() {
          const current = staged.value;
          if (!current || current.preview.python_ok === false) return;
          if (current.preview.change === "same") return; // the server refuses it too
          await run(tr("安装", "Install"), async () => {
            const response = await api.fetch("packages/install", {
              method: "POST",
              body: { token: current.token, allow_network: allowNetwork.value },
            });
            const installed = response?.installed;
            // The token is consumed after success, even if a subsequent refresh fails.
            staged.value = null;
            allowNetwork.value = false;
            if (installed?.id) {
              packages.value = [
                ...packages.value.filter(
                  (item) =>
                    item.name !== installed.name && item.id !== installed.id,
                ),
                installed,
              ];
            }
            api.toast.success(
              tr(
                "技能包已安装；后端已同步 MCP，无需重新加载。",
                "Skill package installed; MCP was synced by the backend.",
              ),
            );
            await Promise.all([refreshPackages(), refreshMcp()]);
          });
        }

        async function remove(item) {
          await run(tr("删除", "Delete"), async () => {
            await api.fetch(`packages/${encodeURIComponent(item.id)}`, {
              method: "DELETE",
            });
            packages.value = packages.value.filter((pkg) => pkg.id !== item.id);
            deleteId.value = null;
            if (readme.value?.id === item.id) readme.value = null;
            api.toast.success(
              tr(
                "技能包已删除；后端已同步 MCP。",
                "Skill package deleted; MCP was synced by the backend.",
              ),
            );
            await Promise.all([refreshPackages(), refreshMcp()]);
          });
        }

        async function showReadme(item) {
          await run(tr("读取 README", "Read README"), async () => {
            const response = await api.fetch(
              `packages/${encodeURIComponent(item.id)}/readme`,
            );
            // JSON {readme} is the host API contract; also tolerate string adapters.
            const content =
              typeof response === "string" ? response : response?.readme;
            if (typeof content !== "string")
              throw new Error(
                tr(
                  "README 接口需返回 JSON {readme}；可先在上传预览中查看。",
                  "The README endpoint must return JSON {readme}. You can view it in the upload preview.",
                ),
              );
            readme.value = {
              id: item.id,
              title: `${text(item.name)} ${text(item.version)}`,
              content,
            };
          });
        }

        async function enableMcp() {
          await run(tr("开启 MCP", "Enable MCP"), async () => {
            await api.setMcpEnabled(true);
            mcpEnabled.value = true;
            mcpError.value = "";
            api.toast.success(
              tr("MCP 总开关已开启。", "Global MCP access is enabled."),
            );
            await refreshMcp();
          });
        }

        const button = (label, onClick, extra = {}) =>
          h(
            "button",
            {
              type: "button",
              disabled: Boolean(busy.value),
              onClick,
              ...extra,
              style: {
                padding: "5px 10px",
                cursor: busy.value ? "wait" : "pointer",
              },
            },
            label,
          );
        const field = (label, value) =>
          h("div", { style: { margin: "4px 0", overflowWrap: "anywhere" } }, [
            h("strong", `${label}${isEnglish.value ? ": " : "："}`),
            text(value),
          ]);
        const requirements = (label, values) =>
          h("div", [
            h("strong", `${label}${isEnglish.value ? ": " : "："}`),
            list(values).length
              ? h(
                  "ul",
                  list(values).map((value, index) =>
                    h("li", { key: index }, text(value)),
                  ),
                )
              : tr("无", "None"),
          ]);

        function previewPanel() {
          if (!staged.value) return null;
          const p = staged.value.preview;
          const same = p.change === "same";
          const old = list(p.replaces).join(isEnglish.value ? ", " : "、");
          const banner = {
            same: tr(
              `“${text(p.name)}” ${text(p.version)} 已安装，不能重复安装。如需重装，请先删除已安装的版本。`,
              `${text(p.name)} ${text(p.version)} is already installed and cannot be installed again. To reinstall it, delete the installed version first.`,
            ),
            upgrade: tr(
              `升级：已安装 ${old}，安装后将被 ${text(p.version)} 取代。`,
              `Upgrade: ${old} is installed and will be replaced by ${text(p.version)}.`,
            ),
            downgrade: tr(
              `降级：已安装 ${old}，安装后将被较旧的 ${text(p.version)} 取代。`,
              `Downgrade: ${old} is installed and will be replaced by the older ${text(p.version)}.`,
            ),
          }[p.change];
          return h(
            "section",
            {
              style: box,
              "aria-label": tr("安装预览", "Installation preview"),
            },
            [
              h("h3", tr("安装预览", "Installation preview")),
              banner
                ? h(
                    "p",
                    {
                      role: same ? "alert" : "status",
                      "data-testid": `qc-change-${p.change}`,
                      style: {
                        padding: "8px 10px",
                        borderRadius: "6px",
                        fontWeight: "bold",
                        border: `1px solid ${same ? "var(--el-color-danger, #b42318)" : "var(--el-color-warning, #b54708)"}`,
                        color: same
                          ? "var(--el-color-danger, #b42318)"
                          : "var(--el-color-warning, #b54708)",
                      },
                    },
                    banner,
                  )
                : null,
              field(tr("名称", "Name"), p.name),
              field(tr("版本", "Version"), p.version),
              field(tr("技能数量", "Number of skills"), p.skill_count),
              field(
                tr("Python 要求", "Python requirement"),
                p.requires_python || tr("未指定", "Not specified"),
              ),
              requirements(tr("依赖安装包", "Required packages"), p.requires),
              requirements(tr("缺少依赖", "Missing dependencies"), p.missing),
              h(
                "strong",
                tr(
                  `ZIP 嵌套层级（${list(p.nesting).length} 层，外 → 内）：`,
                  `ZIP nesting (${list(p.nesting).length} level${list(p.nesting).length === 1 ? "" : "s"}, outer → inner):`,
                ),
              ),
              list(p.nesting).length
                ? h(
                    "ol",
                    list(p.nesting).map((name, index) =>
                      h("li", { key: index }, text(name)),
                    ),
                  )
                : h("p", tr("未提供", "Not provided")),
              h(
                "details",
                { key: staged.value.token, style: { margin: "16px 0" } },
                [
                  h(
                    "summary",
                    { style: { cursor: "pointer", fontWeight: "bold" } },
                    tr("README（纯文本）", "README (plain text)"),
                  ),
                  h(
                    "pre",
                    { style: preStyle },
                    typeof p.readme === "string" && p.readme
                      ? p.readme
                      : tr("此包未提供 README。", "No README was provided."),
                  ),
                ],
              ),
              h(
                "p",
                tr(
                  "安装将运行包及其依赖的代码。请确认来源可信后继续。安装成功后，后端自动同步 MCP。",
                  "Installation will run code from this package and its dependencies. Continue only if you trust the source. MCP will be synced by the backend after installation.",
                ),
              ),
              list(p.missing).length
                ? h(
                    "p",
                    tr(
                      "缺少依赖；离线安装可能失败。如需下载，请明确允许联网（使用应用配置的代理）。",
                      "Dependencies are missing; offline installation may fail. To download them, explicitly allow network access (the app's configured proxy will be used).",
                    ),
                  )
                : null,
              h("label", { style: row }, [
                h("input", {
                  type: "checkbox",
                  checked: allowNetwork.value,
                  disabled: Boolean(busy.value),
                  onChange: (event) => {
                    allowNetwork.value = event.target.checked;
                  },
                }),
                tr(
                  "允许联网下载依赖（默认关闭）",
                  "Allow network access to download dependencies (off by default)",
                ),
              ]),
              p.python_ok === false
                ? h(
                    "p",
                    { role: "alert" },
                    tr(
                      "当前 Python 版本不满足要求，不能安装。",
                      "The current Python version does not meet the requirement; installation is unavailable.",
                    ),
                  )
                : null,
              h("div", { style: { ...row, marginTop: "12px" } }, [
                same
                  ? null
                  : button(
                      p.change === "upgrade"
                        ? tr("确认升级", "Upgrade")
                        : p.change === "downgrade"
                          ? tr("确认降级", "Downgrade")
                          : tr("确认安装", "Install"),
                      install,
                      {
                        disabled: Boolean(busy.value) || p.python_ok === false,
                      },
                    ),
                button(tr("取消", "Cancel"), () => {
                  staged.value = null;
                  allowNetwork.value = false;
                }),
              ]),
            ],
          );
        }

        onMounted(refresh);
        return () =>
          h(
            "div",
            {
              style: { padding: "12px", color: "inherit" },
              "aria-busy": Boolean(busy.value),
            },
            [
              h("section", { style: box }, [
                h(
                  "h3",
                  tr("上传 Qualcomm 技能包", "Upload Qualcomm skill package"),
                ),
                h(
                  "p",
                  tr(
                    "选择 ZIP 文件（支持嵌套 ZIP）。先解析预览，确认后才安装。",
                    "Choose a ZIP file (nested ZIPs are supported). It will be inspected first; installation starts only after confirmation.",
                  ),
                ),
                h("input", {
                  type: "file",
                  accept: ".zip,application/zip",
                  disabled: Boolean(busy.value),
                  "aria-label": tr(
                    "上传 ZIP 技能包",
                    "Upload ZIP skill package",
                  ),
                  onChange: upload,
                }),
              ]),
              h(
                "section",
                {
                  style: box,
                  "aria-label": tr("MCP 总开关", "Global MCP toggle"),
                },
                [
                  h(
                    "p",
                    mcpEnabled.value === true
                      ? tr(
                          "MCP 总开关已开启。",
                          "Global MCP access is enabled.",
                        )
                      : mcpEnabled.value === false
                        ? tr(
                            "MCP 总开关已关闭：已安装技能不会提供给模型。",
                            "Global MCP access is disabled; installed skills are unavailable to the model.",
                          )
                        : tr(
                            "MCP 总开关状态未知。",
                            "Global MCP status is unknown.",
                          ),
                  ),
                  mcpError.value
                    ? h("p", { role: "alert" }, mcpError.value)
                    : null,
                  mcpEnabled.value !== true
                    ? button(tr("一键开启 MCP", "Enable MCP"), enableMcp)
                    : null,
                  h(
                    "p",
                    tr(
                      "技能包安装 / 删除由后端同步 MCP，前端无需主动 reload。",
                      "MCP is synced by the backend when skill packages are installed or deleted; no manual reload is needed.",
                    ),
                  ),
                ],
              ),
              busy.value
                ? h(
                    "p",
                    { role: "status" },
                    isEnglish.value
                      ? `${busy.value} in progress…`
                      : `${busy.value}中，请稍候…`,
                  )
                : null,
              error.value
                ? h(
                    "p",
                    {
                      role: "alert",
                      style: { color: "var(--el-color-danger, #b42318)" },
                    },
                    error.value,
                  )
                : null,
              previewPanel(),
              h("section", { style: box }, [
                h("div", { style: row }, [
                  h("h3", tr("已安装技能包", "Installed skill packages")),
                  button(
                    tr("刷新列表与状态", "Refresh list and status"),
                    refresh,
                  ),
                ]),
                !packages.value.length
                  ? h(
                      "p",
                      loaded.value
                        ? tr(
                            "暂无已安装技能包。",
                            "No skill packages installed.",
                          )
                        : tr(
                            "尚未加载列表。",
                            "The package list has not loaded yet.",
                          ),
                    )
                  : null,
                ...packages.value.map((item) =>
                  h("article", { key: item.id, style: box }, [
                    field(tr("名称", "Name"), item.name),
                    field(tr("版本", "Version"), item.version),
                    field(tr("技能数量", "Number of skills"), item.skill_count),
                    item.summary
                      ? field(tr("说明", "Description"), item.summary)
                      : null,
                    h("div", { style: row }, [
                      button("README", () => showReadme(item)),
                      button(tr("删除", "Delete"), () => {
                        deleteId.value = item.id;
                      }),
                    ]),
                    deleteId.value === item.id
                      ? h("div", { style: { marginTop: "8px" } }, [
                          h(
                            "p",
                            isEnglish.value
                              ? `Delete ${text(item.name)} ${text(item.version)}? Its skills will no longer be available.`
                              : `确认删除 ${text(item.name)} ${text(item.version)}？该包的技能将不再可用。`,
                          ),
                          h("div", { style: row }, [
                            button(tr("确认删除", "Confirm delete"), () =>
                              remove(item),
                            ),
                            button(tr("取消", "Cancel"), () => {
                              deleteId.value = null;
                            }),
                          ]),
                        ])
                      : null,
                  ]),
                ),
              ]),
              readme.value
                ? h(
                    "section",
                    {
                      style: box,
                      "aria-label": tr(
                        "已安装包 README",
                        "Installed package README",
                      ),
                    },
                    [
                      h("div", { style: row }, [
                        h("h3", `${readme.value.title} — README`),
                        button(tr("关闭 README", "Close README"), () => {
                          readme.value = null;
                        }),
                      ]),
                      h(
                        "pre",
                        { style: preStyle },
                        readme.value.content ||
                          tr("此包未提供 README。", "No README was provided."),
                      ),
                    ],
                  )
                : null,
            ],
          );
      },
    },
  });
}
