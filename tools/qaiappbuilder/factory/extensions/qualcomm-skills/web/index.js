// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause

// Route A: use the host's Vue instance; no bare imports or template compiler.
export function activate(api) {
  const { h, ref, onMounted } = api.vue;
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
    title: "Qualcomm 技能包",
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
              mcpEnabled.value === null ? "无法确认 MCP 总开关状态。" : "";
          } catch (err) {
            mcpEnabled.value = null;
            mcpError.value = `读取 MCP 状态失败：${message(err)}`;
          }
        }

        async function refreshPackages() {
          const response = await api.fetch("packages");
          packages.value = list(response?.packages);
          loaded.value = true;
        }

        async function refresh() {
          await run("刷新", async () => {
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
            error.value = "请选择非空 ZIP 文件。";
            api.toast.error(error.value);
            return;
          }
          await run("上传并解析", async () => {
            const response = await api.upload(
              `packages/stage?filename=${encodeURIComponent(file.name)}`,
              file,
            );
            if (!response?.preview || !response?.token)
              throw new Error("服务端未返回有效预览，请重新上传。");
            staged.value = { preview: response.preview, token: response.token };
          });
        }

        async function install() {
          const current = staged.value;
          if (!current || current.preview.python_ok === false) return;
          await run("安装", async () => {
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
            api.toast.success("技能包已安装；后端已同步 MCP，无需重新加载。");
            await Promise.all([refreshPackages(), refreshMcp()]);
          });
        }

        async function remove(item) {
          await run("删除", async () => {
            await api.fetch(`packages/${encodeURIComponent(item.id)}`, {
              method: "DELETE",
            });
            packages.value = packages.value.filter((pkg) => pkg.id !== item.id);
            deleteId.value = null;
            if (readme.value?.id === item.id) readme.value = null;
            api.toast.success("技能包已删除；后端已同步 MCP。");
            await Promise.all([refreshPackages(), refreshMcp()]);
          });
        }

        async function showReadme(item) {
          await run("读取 README", async () => {
            const response = await api.fetch(
              `packages/${encodeURIComponent(item.id)}/readme`,
            );
            // JSON {readme} is the host API contract; also tolerate string adapters.
            const content =
              typeof response === "string" ? response : response?.readme;
            if (typeof content !== "string")
              throw new Error(
                "README 接口需返回 JSON {readme}；可先在上传预览中查看。",
              );
            readme.value = {
              id: item.id,
              title: `${text(item.name)} ${text(item.version)}`,
              content,
            };
          });
        }

        async function enableMcp() {
          await run("开启 MCP", async () => {
            await api.setMcpEnabled(true);
            mcpEnabled.value = true;
            mcpError.value = "";
            api.toast.success("MCP 总开关已开启。");
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
            h("strong", `${label}：`),
            text(value),
          ]);
        const requirements = (label, values) =>
          h("div", [
            h("strong", `${label}：`),
            list(values).length
              ? h(
                  "ul",
                  list(values).map((value, index) =>
                    h("li", { key: index }, text(value)),
                  ),
                )
              : "无",
          ]);

        function previewPanel() {
          if (!staged.value) return null;
          const p = staged.value.preview;
          return h("section", { style: box, "aria-label": "安装预览" }, [
            h("h3", "安装预览"),
            field("名称", p.name),
            field("版本", p.version),
            field("技能数量", p.skill_count),
            field("Python 要求", p.requires_python || "未指定"),
            requirements("依赖 requires", p.requires),
            requirements("缺少依赖 missing", p.missing),
            h(
              "strong",
              `ZIP 嵌套层级（${list(p.nesting).length} 层，外 → 内）：`,
            ),
            list(p.nesting).length
              ? h(
                  "ol",
                  list(p.nesting).map((name, index) =>
                    h("li", { key: index }, text(name)),
                  ),
                )
              : h("p", "未提供"),
            list(p.replaces).length
              ? field("将替换已装版本", p.replaces.join("、"))
              : null,
            h("h4", "README（纯文本）"),
            h(
              "pre",
              { style: preStyle },
              typeof p.readme === "string" && p.readme
                ? p.readme
                : "此包未提供 README。",
            ),
            h(
              "p",
              "安装将运行包及其依赖的代码。请确认来源可信后继续。安装成功后，后端自动同步 MCP。",
            ),
            list(p.missing).length
              ? h(
                  "p",
                  "缺少依赖；离线安装可能失败。如需下载，请明确允许联网（使用应用配置的代理）。",
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
              "允许联网下载依赖（默认关闭）",
            ]),
            p.python_ok === false
              ? h(
                  "p",
                  { role: "alert" },
                  "当前 Python 版本不满足要求，不能安装。",
                )
              : null,
            h("div", { style: { ...row, marginTop: "12px" } }, [
              button("确认安装", install, {
                disabled: Boolean(busy.value) || p.python_ok === false,
              }),
              button("取消预览", () => {
                staged.value = null;
                allowNetwork.value = false;
              }),
            ]),
          ]);
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
                h("h3", "上传 Qualcomm 技能包"),
                h(
                  "p",
                  "选择 ZIP 文件（支持嵌套 ZIP）。先解析预览，确认后才安装。",
                ),
                h("input", {
                  type: "file",
                  accept: ".zip,application/zip",
                  disabled: Boolean(busy.value),
                  "aria-label": "上传 ZIP 技能包",
                  onChange: upload,
                }),
              ]),
              h("section", { style: box, "aria-label": "MCP 总开关" }, [
                h(
                  "p",
                  mcpEnabled.value === true
                    ? "MCP 总开关已开启。"
                    : mcpEnabled.value === false
                      ? "MCP 总开关已关闭：已安装技能不会提供给模型。"
                      : "MCP 总开关状态未知。",
                ),
                mcpError.value
                  ? h("p", { role: "alert" }, mcpError.value)
                  : null,
                mcpEnabled.value !== true
                  ? button("一键开启 MCP", enableMcp)
                  : null,
                h(
                  "p",
                  "技能包安装 / 删除由后端同步 MCP，前端无需主动 reload。",
                ),
              ]),
              busy.value
                ? h("p", { role: "status" }, `${busy.value}中，请稍候…`)
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
                  h("h3", "已安装技能包"),
                  button("刷新列表与状态", refresh),
                ]),
                !packages.value.length
                  ? h(
                      "p",
                      loaded.value ? "暂无已安装技能包。" : "尚未加载列表。",
                    )
                  : null,
                ...packages.value.map((item) =>
                  h("article", { key: item.id, style: box }, [
                    field("名称", item.name),
                    field("版本", item.version),
                    field("技能数量", item.skill_count),
                    item.summary ? field("说明", item.summary) : null,
                    h("div", { style: row }, [
                      button("README", () => showReadme(item)),
                      button("删除", () => {
                        deleteId.value = item.id;
                      }),
                    ]),
                    deleteId.value === item.id
                      ? h("div", { style: { marginTop: "8px" } }, [
                          h(
                            "p",
                            `确认删除 ${text(item.name)} ${text(item.version)}？该包的技能将不再可用。`,
                          ),
                          h("div", { style: row }, [
                            button("确认删除", () => remove(item)),
                            button("取消", () => {
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
                    { style: box, "aria-label": "已安装包 README" },
                    [
                      h("div", { style: row }, [
                        h("h3", `${readme.value.title} — README`),
                        button("关闭 README", () => {
                          readme.value = null;
                        }),
                      ]),
                      h(
                        "pre",
                        { style: preStyle },
                        readme.value.content || "此包未提供 README。",
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
