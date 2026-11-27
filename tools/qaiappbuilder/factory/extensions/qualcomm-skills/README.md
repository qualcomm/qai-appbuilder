# Qualcomm Skills：使用与开发

`qualcomm-skills` 是随应用发布的内置扩展，源码位于
`factory/extensions/qualcomm-skills/`，由真实扩展 host/manager 在原目录加载。
它将高通外部交付 ZIP 中的 MCP wheel 安装为独立包，并动态贡献 MCP 服务，
不是把交付目录当作普通 Skills 扫描根。

## 用户使用

1. 在扩展菜单打开 **Qualcomm Skills** 面板；若没有该入口，在扩展设置中启用此内置扩展。
2. 上传可信来源的非空 ZIP。支持交付 ZIP 中继续嵌套 ZIP，预览展示选中的 wheel、
   名称/版本、嵌套路径、README、文档名称、wheel 内技能数量、Python 要求、
   依赖及缺失依赖、将替换的旧版本。上传只解析与暂存，不执行 wheel、不运行 pip。
3. 阅读 README 和预览，确认来源及 Python 兼容性后安装。只有确实允许下载依赖时，
   才勾选联网授权；面板每次上传/安装后都将授权恢复为未勾选。
4. 安装成功后后端自动重新加载扩展并同步 MCP，无需再次点击 reload 或重启。
   使用技能还需要打开应用的 **MCP 总开关**，且对应服务未被单独禁用。
   配置已注册不等于连接成功；检查 MCP 状态及该包的环境/外部工具要求。
5. 需要时选择扩展参与当前对话。模型通过包的 MCP 工具（例如 `search_skills`、
   `get_skill`）查找并读取需要的技能，而不是把所有 `SKILL.md` 塞入提示词。
6. 删除一个包会先停止该扩展贡献的 MCP 服务、释放文件，再删除包目录，最后重新加载
   并恢复剩余包的服务。此过程会短暂影响同扩展的其他包，不需要客户端额外刷新运行时。

### wheel MCP，而非全量技能扫描

只安装并运行交付中识别到的 MCP wheel 的服务入口，通常为
`python -m <包模块>.server`；需要时入口包含 `--stdio`。
不会运行 ZIP 中任意脚本，也不会将散落的技能目录或 wheel 的 `_skills_bundle`
注册为应用的普通 Skills 扫描根。预览中的技能数来自 wheel 内容，仅用于展示；
真正的检索/读取由 wheel MCP 实现。请只安装可信的高通交付：安装后的 wheel 是可执行代码，
不是安全沙箱，仍可使用当前用户权限访问机器资源。

### 联网授权与依赖

- API 调用者应始终明确传入 `allow_network`；当前 API 省略它时默认 `true`，
  不应把省略字段视为拒绝授权。面板显式传入用户选择，默认 `false`。
- wheel 安装使用应用当前 Python、`pip install --no-deps --target <包目录>/site`。
  先检查应用环境中已安装的 distribution 版本及 requirement marker；已满足的依赖直接复用，
  不重新安装，也不修改应用环境。
- 若有缺失/版本不满足的依赖且未授权联网，安装返回 `qcskills.dependencies`，
  不会静默下载。无缺失依赖的离线安装使用 `--no-index`。
- 授权后仅为缺失的顶层 requirement 单独调用 pip（含 `--only-binary=:all:`），
  依赖及其解析所需的传递依赖安装到该包的独立 `site`，不安装到应用全局环境。
  使用应用实时代理配置；pip 失败信息会隐藏代理凭据。
- 运行时将该包 `site` 放入 `PYTHONPATH`，设置 `PYTHONNOUSERSITE=1`，同时可复用
  应用 Python 环境的依赖。这里只是包目录隔离，不是独立 venv。
- 联网授权控制的是本次 pip 依赖安装，不是 wheel MCP 后续行为的网络防火墙；
  使用服务仍应遵循交付 README 的权限、网络、设备、凭据要求。

### 数据与内置生命周期

数据以实际应用数据根 `<data>` 为准，不写入 `factory/`：

```text
<data>/extensions-data/qualcomm-skills/
  staging/<token>/                 # 上传解包、预览及待安装 wheel
  packages/<package-id>/
    site/                         # wheel 及独立补装依赖
    README.md
    package.json                  # 安装元数据
<data>/config/                    # 扩展开关等持久化配置
```

取消预览删除暂存目录；成功安装清理对应暂存目录；过期暂存会在后续暂存操作中清扫。
同一 distribution 的新版本成功安装后替换旧版；pip 失败保留已安装旧版。
删除包移除其全部安装文件和 MCP 注册。内置扩展 **可停用、不可卸载或被用户包替换**：
`DELETE /api/extensions/qualcomm-skills` 即使带 `purge_data=true` 也会被拒绝，
不能借此清空数据。停用会停止贡献的服务并隐藏 UI/业务路由，但保留包数据；
再次启用自动重新注册已有包。删除包应使用面板或包 DELETE API，而不是直接删除正在使用的目录。

## 开发/API

扩展使用 `qai.ext` SDK；入口 `qcskills.activate(ctx)` 注册 manifest 中声明的路由，
并按 `Packages.server_specs()` 的当前安装结果调用 `ctx.register_mcp_server()`。
manifest 的 `dynamic_mcp_servers: true` 允许这些运行时贡献，最终服务名为
`qualcomm-skills-<package-server-name>`。贡献配置由扩展持有，不作为用户自建配置写入
`mcp_servers.json`，实际连接由应用 MCP registry 和总开关管理。

管理/UI API：

- `GET /api/extensions`：状态记录中的 `builtin: true`。
- `GET /api/extensions/ui`：已加载扩展的入口及带版本参数的 JS 模块 URL。
- `GET /api/ext/qualcomm-skills/_ui/index.js`：扩展前端模块；使用宿主 Vue/扩展 API，
  不引入第二个 Vue 实例。
- `POST /api/extensions/qualcomm-skills/disable`、`.../enable`：持久化开关。

业务 API 前缀：`/api/ext/qualcomm-skills`。这些都是应用普通 API 路由，正式应用仍应用
登录、CSRF 等中间件；不是独立无鉴权服务。

| 方法与路径 | 请求/返回 |
| --- | --- |
| `GET packages` | `{"packages": [...]}` |
| `POST packages/stage?filename=delivery.zip` | **原始 ZIP 字节**，`Content-Type: application/zip`，不是 multipart/JSON；返回 `{"token": "...", "preview": {...}}`，preview 内也有同一 token |
| `DELETE packages/stage/{token}` | 取消暂存，204 |
| `POST packages/install` | JSON `{"token": "...", "allow_network": false}`；201 `{"installed": {...}}` |
| `DELETE packages/{package_id}` | `{"removed": "...", "reload": true}`；后端已完成 runtime reload |
| `GET packages/{package_id}/readme` | `{"readme": "..."}` |

上传声明单路由 `max_body_mb: 500`；解包仍有独立的大小、文件数、嵌套深度及安全路径检查。
stage 没有 `reload_runtime`，预览不会停止 MCP。前端应读取嵌套的 `response.preview`，
不能把整个响应当成预览，也不应依赖 DELETE 返回的 `reload` 字段自行触发第二次 reload。

### 通用 `reload_runtime`

安装及包删除路由在 `extension.json` 声明 `reload_runtime: true`。
这是通用扩展路由机制，不是路由层对 `qualcomm-skills` 的名称硬编码：

1. 路由层保留当前 handler，并串行化该运行时变更。
2. manager 卸载扩展，然后 `await manager.sync_runtime()`，先让 MCP registry 停止贡献服务。
3. 调用保留的 handler，在后台线程进行同步安装/删除操作。
4. 在 `finally` 中重新加载扩展，再同步 MCP registry；即使安装返回错误，也恢复已有包服务。

扩展 handler 只负责包操作，不直接操作 MCP registry，也不用在 handler 内调用自身 reload。
当其他扩展修改运行中进程占用的文件或贡献服务集合时，可复用同一声明机制。

### 离线定向测试与 lint

在项目根，使用已有、包含项目测试依赖的 Python 环境（不要为这些测试联网安装依赖）：

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD"
& .\envs\.venv_arm64_313\Scripts\python.exe -m pytest tests/unit/factory/test_qualcomm_skills_packages.py tests/unit/factory/test_qualcomm_skills_routes.py -q
& .\envs\.venv_arm64_313\Scripts\python.exe -m ruff check tests/unit/factory/test_qualcomm_skills_routes.py
& .\envs\.venv_arm64_313\Scripts\python.exe -m ruff format --check tests/unit/factory/test_qualcomm_skills_routes.py
```

路由测试复用 package 测试的合成嵌套 ZIP、wheel 和 FakePip，使用真实 `Container`、
composition-root manager/host、FastAPI 路由及 MCP registry。仅替换 pip subprocess 边界；
MCP 总开关关闭以避免执行 synthetic wheel，外部网络 socket 连接被拒绝
（仅允许 Windows asyncio 内部 socketpair 所需的本机回环连接）。
测试验证内置不可卸、raw upload 嵌套预览、自动注册/删除 MCP、删除前停服、
停用/启用保留数据、失败安装后恢复服务；不启动完整应用 lifespan，也不访问网络。
