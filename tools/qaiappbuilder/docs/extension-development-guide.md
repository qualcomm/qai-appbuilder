# AppBuilder 扩展开发指南

本文教你给 AppBuilder 写一个自己的扩展：从最小的「一句话」扩展，到带工具、技能、提示词、设置项、按钮对话框、HTTP 接口和自带前端面板的完整扩展。每一步都有可以直接复制运行的例子。

> **适用版本**：清单格式 `manifest_version: 1`。本文中的清单字段、`ctx` 接口、前端 `api` 接口都以当前代码为准。

---

## 1. 扩展是什么

扩展是一个**文件夹**（或它打成的 `.zip`），里面至少有一个 `extension.json`。AppBuilder 启动时扫描扩展目录，把扩展带来的内容接入应用：

| 扩展可以带来 | 作用 |
|---|---|
| **工具（tools）** | 模型在对话里可以调用的函数，用 Python 写 |
| **技能（skills）** | 一份 `SKILL.md`，告诉模型在某类任务里怎么做；和应用自带技能同一种格式 |
| **提示词（prompts）** | 追加给模型的说明文字，例如这个扩展的身份、使用规则 |
| **设置项（settings）** | 设置页自动生成的表单；密码类字段加密保存 |
| **MCP 服务器（mcp_servers）** | 随扩展启停的 MCP 服务器 |
| **按钮和对话框（ui.actions）** | 输入框下面的按钮，点开是由清单声明的表单，提交后调用你的 Python 函数 |
| **HTTP 接口（routes）** | `/api/ext/<扩展名>/…` 下的接口，登录和 CSRF 保护都已经带上 |
| **前端面板（ui.module）** | 自带一个 JavaScript 模块，在应用页面里渲染自己的界面 |

**这些都是可选的。** 一个扩展可以只带一个技能，也可以什么代码都不写。只有需要运行 Python 时才需要 `entry`。

### 信任模型（请先读这一段）

**扩展和应用拥有同样的权限**：扩展的 Python 代码运行在应用进程里，前端模块运行在应用页面里，可以读写用户能读写的文件、访问网络。它不是沙箱。所以：

- 只安装你信任的来源的扩展；
- 安装时应用会弹出确认框，列出这个扩展会带来什么（工具、技能、是否运行代码、MCP 服务器等），**确认前不会运行任何扩展代码**；
- 自己写扩展时，不要把密码写进代码或 `config.toml`，用设置项里的 `secret` 类型（见 §7）。

---

## 2. 五分钟：第一个扩展

做一个只有一个工具的扩展：让模型可以“回声”一段文字。

### 2.1 目录结构

```
hello-ext/
├── extension.json
└── main.py
```

> **文件夹名必须和清单里的 `name` 一模一样。**

### 2.2 `extension.json`

```json
{
  "manifest_version": 1,
  "name": "hello-ext",
  "version": "1.0.0",
  "description": "Echo text back to the model.",
  "entry": "main.py",
  "tools": [
    { "name": "hello_echo", "group": "read" }
  ]
}
```

### 2.3 `main.py`

```python
def activate(ctx):
    def echo(call):
        return f"Hello, {call.arguments.get('text', '')}!"

    ctx.register_tool(
        "hello_echo",
        echo,
        description="Echo the given text back with a greeting.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    )
```

### 2.4 装上它

三种方式，任选其一：

1. **设置 → Extensions → Add folder…**：弹出系统的选择文件夹对话框，选 `hello-ext`。
2. **设置 → Extensions → Add .zip…**：先把 `hello-ext` 文件夹压成 zip（`extension.json` 放在 zip 根目录，或放在 zip 里**唯一**的一层文件夹里），再选这个 zip。
3. 直接把文件夹复制到扩展目录（设置页顶部的 **Folder** 一行显示了具体路径，通常是 `<数据目录>\extensions\`），然后点 **Reload all**。

用方式 1、2 时，应用会先弹出确认框，显示工具、技能等清单，点 **Install** 才真正安装并加载。安装后在扩展列表里看到 `hello-ext` 状态为 **Enabled**，就已经生效了，不需要重启。

在聊天里对模型说“用 hello_echo 工具对 world 打个招呼”，模型就会调用它。

### 2.5 改了代码之后

改完 `main.py` 或 `extension.json`，在扩展卡片上点 **Reload**（或 **Reload all**）即可，不用重启应用。加载失败时，卡片上会显示具体原因。

---

> 本文的所有示例（§2 的 `hello-ext` 和 §7 的 `weather-ext`、§9 的前端模块）都是从本文直接提取出来、在真实的应用后端和浏览器里装过、跑过的：能安装、工具能被调用、按钮能提交、设置里的密码只写不读、前端面板能渲染。

## 3. 目录结构全貌

```
my-ext/
├── extension.json          # 必须：清单
├── main.py                 # 入口：一个 .py 文件，或者一个带 __init__.py 的包目录
├── config.toml             # 可选：扩展自己的只读默认配置
├── skills/                 # 可选：技能，每个技能一个子目录
│   └── my-skill/
│       └── SKILL.md
├── prompts/                # 可选：提示词文件
│   └── active.md
└── web/                    # 可选：前端模块，只有这个目录会被浏览器访问
    └── index.js
```

扩展安装在 `<数据目录>\extensions\<扩展名>\`。扩展**自己要写入的数据**不要放在这里，用 `ctx.data_dir`（见 §6.2）：它在 `<数据目录>\extensions-data\<扩展名>\`，更新、重装扩展时不会丢。

---

## 4. 清单 `extension.json` 完整参考

**未知字段会报错**（防止拼错字段名后功能悄悄失效）。

### 4.1 顶层字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `manifest_version` | 是 | 固定为 `1` |
| `name` | 是 | 小写字母、数字、连字符，字母开头，如 `my-ext`；最长 64；**必须等于文件夹名** |
| `version` | 是 | 非空字符串，如 `1.0.0`（用来识别升级） |
| `description` | 是 | 字符串，或多语言对象 `{"en": "...", "zh-CN": "...", "zh-TW": "..."}` |
| `entry` | 否 | Python 入口：相对路径，指向一个 `.py` 文件或一个带 `__init__.py` 的目录。声明了 `tools`、`ui.actions`、`routes`、`dynamic_mcp_servers` 就必须有它 |
| `tools` | 否 | 见 §4.2 |
| `skills` | 否 | 见 §4.3 |
| `prompts` | 否 | 见 §4.4 |
| `settings` | 否 | 见 §4.5 |
| `mcp_servers` | 否 | 见 §4.6 |
| `dynamic_mcp_servers` | 否 | `true` 表示代码会在运行时再添加 MCP 服务器；安装确认框会提示用户 |
| `ui` | 否 | 见 §4.7 |
| `routes` | 否 | 见 §4.8 |

### 4.2 `tools`：声明工具

```json
"tools": [
  { "name": "weather_forecast", "group": "read" },
  { "name": "weather_clear_cache", "group": null }
]
```

- `name`：小写字母、数字、下划线，字母开头，**不能以下划线结尾**，不能含连续两个下划线（`__` 留给 MCP 工具），最长 64。
- `group`：**必填**，取 `read` / `edit` / `command` / `null`。这是工具的**权限分组**：用户给对话选了某种角色（例如只读角色）时，应用按分组决定这个工具能不能用。
  - `read`：只读，不改任何东西（查询、搜索）
  - `edit`：会修改文件或数据
  - `command`：会执行命令或有副作用
  - `null`：不归任何分组，不受角色的分组开关影响
  
  强制填写，是为了让你有意识地决定它的权限属性。**如实选择**，不确定就选更严格的一档。
- 工具名保持你写的样子，不会加前缀。**如果和内置工具、MCP 工具或别的扩展的工具重名，只有这一个工具被拒绝**，并在扩展状态里给出警告，扩展其余部分照常加载。所以名字要有辨识度，例如 `weather_forecast` 而不是 `search`。

### 4.3 `skills`：技能

```json
"skills": {
  "dir": "skills",
  "default_modes": { "my-skill": "both" }
}
```

- `dir`：技能目录，默认就是 `skills`，可以不写整个 `skills` 字段。
- `default_modes`：技能 ID → 默认模式，取 `off` / `cloud` / `local` / `both`。用户在技能页改过的模式优先。
- 技能放在 `<dir>/<技能ID>/SKILL.md`，技能 ID 就是文件夹名。**技能 ID 与应用自带技能或别的扩展重复时，只拒绝这一个技能**。

`SKILL.md` 的格式：

```markdown
---
name: my-skill
description: 一句话说明什么时候用这个技能。模型靠这句话决定要不要读它。
---

# 我的技能

正文：告诉模型具体怎么做，步骤、注意事项、示例命令……
```

> 技能目录里的 `skill.policy.json` 不被支持，会被忽略并给出警告。技能里的脚本只在技能被执行时才用到，和扩展的安装、卸载无关。

### 4.4 `prompts`：追加提示词

```json
"prompts": [
  { "when": "always", "text": "When the user asks about weather, prefer the weather_forecast tool." },
  { "when": "active", "file": "prompts/active.md" }
]
```

- `when`：
  - `always`：**每一轮对话**都追加到系统提示词末尾。只在安装、重载扩展时才会变化，所以不会破坏提示词缓存。
  - `active`：**只有用户在输入框里选中了这个扩展**时才发给模型，放在上下文消息里，不进系统提示词。适合“身份与使用规则”。
- `text`（直接写）和 `file`（读一个 UTF-8 文本文件）**二选一**。
- 每条最长 8000 字符。文件找不到、不是 UTF-8、超长，该扩展加载失败。

提示词只是追加，**不会替换系统身份**。

### 4.5 `settings`：设置项

```json
"settings": [
  { "key": "endpoint", "type": "text", "label": "API endpoint",
    "default": "https://api.example.com", "required": true },
  { "key": "api_token", "type": "secret", "label": "API token" },
  { "key": "verbose", "type": "bool", "label": "Verbose logging", "default": false },
  { "key": "timeout", "type": "number", "label": "Timeout (s)", "default": 10, "min": 1, "max": 120 },
  { "key": "region", "type": "select", "label": "Region", "options": ["cn", "us"], "default": "cn" }
]
```

| 字段 | 说明 |
|---|---|
| `key` | 必填。小写字母、数字、下划线，字母开头，最长 64，同一扩展内不重复 |
| `type` | 必填：`text` / `secret` / `bool` / `number` / `select` |
| `label` | 必填。界面显示的名字 |
| `description` | 可选。说明文字 |
| `default` | 可选。**`secret` 不允许有默认值** |
| `required` | 可选，布尔 |
| `options` | `select` 必填：非空、互不重复的字符串列表；其他类型不能写 |
| `min` / `max` | 只对 `number` 有效 |

设置项会自动出现在扩展卡片的 **Settings** 里。

- `secret` 类型的值**只写不读**：界面永远不会把已保存的值显示出来，只显示“已设置”。它们保存在扩展自己的密钥空间里，用 `ctx.secrets` 读（§6.3）。
- 其他类型在代码里用 `ctx.settings` 读，**每次访问都读最新值**，用户改了设置不用重载就生效。

### 4.6 `mcp_servers`：随扩展启停的 MCP 服务器

```json
"mcp_servers": [
  {
    "name": "files",
    "transport": "stdio",
    "command": "${python}",
    "args": ["${root}/server/run.py", "--data", "${data_dir}"],
    "env": { "API_TOKEN": "${secret:api_token}" },
    "timeout_s": 30
  }
]
```

- `name`：同扩展名规则。应用里的完整名称是 `<扩展名>-<name>`。
- `transport`：`stdio`（默认）、`http`、`sse`。
  - `stdio`：必须有 `command`；可以有 `args`、`env`、`cwd`；**不能**有 `url`、`headers`。
  - `http` / `sse`：必须有 `url`；可以有 `headers`；**不能**有 `command`、`args`、`env`、`cwd`。
- `timeout_s`：正数，默认 30。
- 所有字符串里可以使用占位符：

| 占位符 | 含义 |
|---|---|
| `${root}` | 扩展自己的目录 |
| `${data_dir}` | 扩展的数据目录 |
| `${python}` | 应用正在使用的 Python |
| `${setting:KEY}` | 某个**非密码**设置项的当前值 |
| `${secret:KEY}` | 某个 `secret` 设置项的值（用在 `env`、`headers` 里最合适） |

- 这些服务器**只读展示**：用户不能在 MCP 面板里编辑或删除它们，只能启停；它们随扩展一起加载和卸载。
- 用到的密码项还没填时，只跳过这一个服务器并给出警告，填好后自动启动。

如果服务器要到运行时才能确定（例如要先解压一个包才知道命令在哪），在清单里写 `"dynamic_mcp_servers": true`，然后在 `activate` 里调用 `ctx.register_mcp_server(...)`（§6.4）。

### 4.7 `ui`：输入框里的入口、按钮和前端面板

只要写了 `ui`，扩展就会出现在输入框下面的 **Extension ▾** 菜单里。选中后，左下角出现和内置模式一样的高亮标签，标签右边是你声明的按钮。

```json
"ui": {
  "label": "Remote Host",
  "icon": "🖥",
  "actions": [
    {
      "id": "authorize",
      "label": "Authorize host",
      "description": "Grant access to a host",
      "fields": [
        { "key": "host", "type": "text", "label": "Host", "required": true },
        { "key": "password", "type": "secret", "label": "Password" }
      ]
    }
  ],
  "module": "web/index.js"
}
```

| 字段 | 说明 |
|---|---|
| `label` | 必填。菜单里和标签上显示的名字。可以写成字符串，也可以按语言写成 `{"en": "System Info", "zh-CN": "本机信息", "zh-TW": "本機資訊"}`，应用按界面语言显示（没有当前语言就用 `zh-CN`/`zh-TW`/`en` 里能找到的；写成字符串则所有语言都显示它） |
| `icon` | 可选。一个 emoji，最长 8 个字符 |
| `actions` | 可选。**最多 8 个**按钮 |
| `module` | 可选。前端模块，见 §9 |

`actions[]` 的每一项：

- `id`：小写字母、下划线、数字，字母开头，最长 64，**不能重复**。
- `label`：按钮上的文字，和 `ui.label` 一样可以写成字符串或按语言的对象；`description`：鼠标悬停说明。
- `fields`：对话框里的表单字段，写法和 `settings[]` **完全一样**（类型、`required`、`default`、`options`、`min`/`max`）。`secret` 字段不会被保存，只在这一次提交时交给你的函数。

点击按钮 → 弹出由 `fields` 生成的表单 → 提交 → 应用先按声明校验输入（不合法直接提示，不会进入你的代码）→ 调用你注册的处理函数（§6.5）。

**没有 `fields` 的按钮不弹对话框**：点一下就直接运行处理函数。适合“让助手做某件事”的按钮：处理函数返回 `ActionResult(prompt="...")`，这段话就作为用户的下一条消息发出（内置的「本机信息」扩展的“概览”“对外端口”按钮就是这样做的）。

> 没有 `ui.actions` 也可以只写 `ui.label` / `ui.module`。如果只想要 **Settings** 面板（自己画界面），见 §9。

### 4.8 `routes`：HTTP 接口

```json
"routes": [
  { "method": "GET",  "path": "status" },
  { "method": "POST", "path": "hosts/{host_id}/check" },
  { "method": "POST", "path": "upload", "max_body_mb": 200 }
]
```

- 地址是 `/api/ext/<扩展名>/<path>`。例如上面第二条是 `POST /api/ext/my-ext/hosts/abc/check`。
- `method`：`GET` / `POST` / `PUT` / `PATCH` / `DELETE`。
- `path`：用 `/` 分隔的段，每段是小写字母、数字、`_`、`.`、`-`，或 `{参数名}`（匹配一段）；**不能以 `/` 开头或结尾**，不能以下划线开头（`_ui` 是保留的）；参数名不重复；最长 200；最多 32 条路由。两条路由的“形状”相同会冲突。
- `max_body_mb`：这条路由接受的请求体上限，默认 10，最大 1024。
- `reload_runtime`：设为 `true` 时，处理这个请求前先停掉本扩展带来的服务（子进程），处理完再重新加载，适合“会替换扩展自己文件”的操作。普通扩展用不到。

这些接口和应用其它 `/api` 接口一样受登录和 CSRF 保护，**不需要也不应该自己再做认证**。

---

## 5. 入口文件：`activate(ctx)`

入口必须定义一个**普通（非 async）**函数 `activate(ctx)`。应用加载扩展时调用它**一次**，你在里面**注册**工具、按钮处理函数、路由等。

```python
def activate(ctx):
    ...  # 在这里注册
```

要点：

- 注册只能在 `activate` **执行期间**做。之后再调用 `register_*` 会报错。
- `activate` 抛出任何异常，这个扩展加载失败：应用会把你已经登记的东西全部撤掉，运行你注册过的 `on_unload` 回调，不留任何残留。失败原因显示在扩展卡片上。
- 加载失败只影响这一个扩展，不影响其它扩展和应用启动。
- 入口会以私有模块名导入，不会改动 `sys.path`。要在多个文件之间拆分代码，把入口做成**包目录**（`"entry": "mypkg"`，里面有 `__init__.py`），包内用相对导入：`from .helpers import foo`。
- 每次重载都会重新导入你的代码，所以模块里的全局状态在重载时会丢，需要持久的东西放到 `ctx.data_dir`。

### 只允许从 `qai.ext` 导入

扩展**唯一**可以从应用导入的模块是 `qai.ext`：

```python
from qai.ext import (
    ExtensionContext,   # ctx 的类型，用于类型标注
    ExtensionError,     # 接口被误用时抛出，可以捕获
    ToolCall,           # 工具处理函数收到的参数
    ActionCall, ActionResult,   # 按钮处理函数
    RouteRequest, RouteResponse,  # 路由处理函数
    ExtensionSecrets,   # ctx.secrets 的类型
)
```

这些导入**只是为了类型标注和捕获异常**，不导入也能工作（§2 的例子就没有任何导入）。**不要**导入 `qai.chat`、`apps`、`interfaces` 等其它内部模块：它们随时可能改变，不保证兼容。你需要的一切都从 `ctx` 和处理函数的参数里拿。

---

## 6. `ctx` 接口

`ctx` 是应用传给 `activate` 的对象。它是你和应用之间的**唯一桥梁**。

### 6.1 基本信息

| 成员 | 说明 |
|---|---|
| `ctx.name` | 扩展名（清单的 `name`） |
| `ctx.version` | 清单的 `version` |
| `ctx.root` | 扩展自己的目录（`Path`）。**只读**，不要往里面写 |
| `ctx.logger` | 标准 `logging.Logger`，名字是 `qai.user_extension.<name>`，写入应用日志 |
| `ctx.proxy_url()` | 应用当前使用的代理地址（可能含用户名密码，**不要打日志**），没有则 `None` |
| `ctx.ssl_verify` | 应用当前是否校验 TLS 证书；每次请求前读，不要缓存 |

### 6.2 数据与配置

| 成员 | 说明 |
|---|---|
| `ctx.data_dir` | 扩展的**可写**数据目录（`Path`，首次访问时创建）。卸载时用户可以选择一并删除。 |
| `ctx.config` | 解析后的 `<扩展>/config.toml`（只读映射）；没有这个文件则为 `{}`。适合放不让用户改的默认值，例如接口地址 |
| `ctx.settings` | 用户在设置页填的值（见 §4.5）。每个**非密码**项都存在：用户的值，否则默认值，否则 `None`。每次访问都是最新的 |

`config.toml` 写错会让读取它的代码抛 `ExtensionError`。

### 6.3 密钥：`ctx.secrets`

```python
token = ctx.secrets.get("api_token")   # 没有则 None
ctx.secrets.set("session", "abc")      # 值必须是字符串
ctx.secrets.delete("session")          # 不存在也不报错
ctx.secrets.keys()                     # 已存的 key 列表，不含值
```

- 每个扩展有自己独立的密钥空间，**互相看不到**。
- key 不允许包含 `/`、`\`、`..`、空字符，否则抛 `ExtensionError`。
- 应用没有可用的密钥存储时抛 `ExtensionError`，请捕获并给用户一个提示。
- 用户在设置页填的 `secret` 项，就在这里读。

### 6.4 注册工具

```python
ctx.register_tool(name, handler, *, description, parameters=None)
```

- `name`：必须已在清单 `tools` 里声明。
- `description`：非空字符串。**这是模型决定“要不要调用、怎么调用”的主要依据，请写清楚**。
- `parameters`：JSON Schema 对象（`{"type": "object", "properties": {...}, "required": [...]}`）。省略表示没有参数。必须能序列化成 JSON。
- `handler(call)`：普通函数或 `async` 函数。
  - **普通函数在工作线程里运行**，阻塞的网络、磁盘调用不会卡住聊天。
  - **`async` 函数在事件循环里运行**，你要自己避免阻塞。
- `call` 是 `ToolCall`（只读）：

| 字段 | 说明 |
|---|---|
| `call.arguments` | 模型传来的参数（字典，每次调用都是新的） |
| `call.tool_name` | 正在被调用的工具名 |
| `call.conversation_id` | 来自哪个对话（不透明字符串，只当作键用） |
| `call.tab_id` | 来自哪个标签页 |

返回值就是工具结果，交还给模型。**返回字符串最稳妥**；要返回结构化数据，自己用 `json.dumps(...)` 转成字符串，模型更容易读。出错时返回一段让模型能看懂的文字（例如 `"Error: city not found"`），或者直接抛异常。

注册同一个工具两次、注册未声明的工具、`parameters` 不合法，都会让扩展加载失败。

### 6.5 注册按钮处理函数

```python
ctx.register_action(action_id, handler)
```

`action_id` 必须是清单 `ui.actions` 里声明的 `id`。`handler(call)` 收到 `ActionCall`：

| 字段 | 说明 |
|---|---|
| `call.action_id` | 动作 id |
| `call.values` | 表单的值，**已按清单校验过**，缺省项补了默认值。`secret` 项的值也在里面，宿主不保存 |
| `call.conversation_id` / `call.tab_id` | 用户点击时所在的对话和标签页（可能为空） |

返回值：

- 一个字符串：作为消息提示给用户；
- `None`：什么都不提示；
- `ActionResult(message="...", prompt="...", ok=True)`：
  - `message`：提示给用户；
  - `prompt`：**非空时，会作为用户的下一条聊天消息发出**。这是“让按钮触发一次对话”的方法，例如“授权成功，请检查主机 h1”；
  - `ok=False`：把 `message` 当成错误显示。
- 抛异常：错误信息显示给用户，应用不受影响。

普通函数在工作线程里运行，`async` 函数在事件循环里运行。

### 6.6 注册 HTTP 路由

```python
ctx.register_route(method, path, handler)
```

`method` 和 `path` 必须和清单 `routes` 里声明的一致。`handler(req)` 收到 `RouteRequest`：

| 字段 | 说明 |
|---|---|
| `req.method` / `req.path` | 请求方法和路径 |
| `req.params` | `{参数}` 的值，例如 `req.params["host_id"]` |
| `req.query` | 查询字符串（同名取最后一个值） |
| `req.body` | 解析后的 JSON 请求体；请求体为空或不是 JSON 则为 `None` |
| `req.raw_body` | 原始请求字节（上传文件时用） |
| `req.content_type` | 原始 `Content-Type` |
| `req.headers` | 小写的请求头；**不含** Cookie、Authorization、CSRF、Proxy-Authorization（应用自己的凭据不会交给你） |

返回值：

- 任何可以转成 JSON 的值 → `200` JSON；
- `None` → `204`；
- `RouteResponse(body=..., status=200, content_type="application/json", headers={})` → 完全自定义。`body` 是 `str` 或 `bytes` 时原样发送，并使用 `content_type`。

处理函数抛异常：返回 `500`，错误信息是 `异常类型: 文字`，应用不受影响。

**返回错误时的格式很重要**：前端只会把应用标准错误格式里的 `message` 显示给用户。如果你想返回一个带说明的错误，请用：

```python
return RouteResponse(
    body={"type": "ExtensionRouteError", "code": "my.some_code", "message": "给用户看的文字"},
    status=400,
)
```

不按这个格式返回，页面只会显示笼统的 “HTTP 400 returned a non-conforming error body”，看不到你的说明。

### 6.7 动态 MCP 服务器

```python
ctx.register_mcp_server(
    "files",                      # 应用里的名字是 <扩展名>-files
    command="python",
    args=["-m", "my_server"],
    env={"TOKEN": token},
    secret_env=["TOKEN"],         # 这些 env 的值是密钥，展示时会被遮住
)
```

- 清单必须写 `"dynamic_mcp_servers": true`，否则抛 `ExtensionError`。
- 规则和 `mcp_servers[]` 相同，但**值是字面量，不使用 `${...}` 占位符**（你的代码自己知道路径）。
- `stdio` 需要 `command`；`http`/`sse` 需要 `url`。`timeout_s` 在 (0, 600]。
- 同样是只读展示、随扩展一起卸载。

### 6.8 清理：`on_unload`

```python
ctx.on_unload(callback)
```

扩展被卸载、重载、或加载失败时运行 `callback`（先注册的后运行，抛异常只记日志）。用来关闭后台线程、删除临时文件等。

### 6.9 异常

`ExtensionError` 表示接口被误用或做不到：注册未声明的东西、`config.toml` 格式错误、密钥的 key 不合法、没有密钥存储等。在 `activate` 里抛出会让扩展加载失败；在处理函数里可以捕获。

---

## 7. 完整示例：天气扩展

把前面的内容合起来：一个带设置、密码、工具、技能、提示词、按钮的扩展。

```
weather-ext/
├── extension.json
├── config.toml
├── main.py
├── prompts/
│   └── active.md
└── skills/
    └── weather-report/
        └── SKILL.md
```

**`extension.json`**

```json
{
  "manifest_version": 1,
  "name": "weather-ext",
  "version": "1.0.0",
  "description": {
    "en": "Look up weather forecasts.",
    "zh-CN": "查询天气预报。"
  },
  "entry": "main.py",
  "tools": [
    { "name": "weather_forecast", "group": "read" }
  ],
  "skills": {
    "default_modes": { "weather-report": "both" }
  },
  "prompts": [
    { "when": "active", "file": "prompts/active.md" }
  ],
  "settings": [
    { "key": "api_token", "type": "secret", "label": "API token", "required": true },
    { "key": "units", "type": "select", "label": "Units", "options": ["metric", "imperial"], "default": "metric" }
  ],
  "ui": {
    "label": "Weather",
    "icon": "🌦",
    "actions": [
      {
        "id": "ask_city",
        "label": "Ask about a city",
        "fields": [
          { "key": "city", "type": "text", "label": "City", "required": true }
        ]
      }
    ]
  }
}
```

**`config.toml`**

```toml
endpoint = "https://api.example.com/weather"
```

**`prompts/active.md`**

```markdown
You are helping the user with weather questions. Prefer the `weather_forecast` tool over guessing,
and always say which units the numbers are in.
```

**`skills/weather-report/SKILL.md`**

```markdown
---
name: weather-report
description: Use when the user wants a short weather report for a city, including what to wear.
---

# Weather report

1. Call `weather_forecast` for the city.
2. Summarize temperature, rain chance and wind in two sentences.
3. Suggest what to wear.
```

**`main.py`**

```python
import json
import urllib.parse
import urllib.request

from qai.ext import ActionCall, ActionResult, ExtensionContext, ExtensionError, ToolCall


def activate(ctx: ExtensionContext) -> None:
    endpoint = ctx.config.get("endpoint", "https://api.example.com/weather")

    def forecast(call: ToolCall) -> str:           # 普通函数：在工作线程里运行
        city = str(call.arguments.get("city", "")).strip()
        if not city:
            return "Error: 'city' is required."
        try:
            token = ctx.secrets.get("api_token")
        except ExtensionError as exc:              # 例如应用没有可用的密钥存储
            return f"Cannot read the API token: {exc}"
        if not token:
            return "Please set the API token in the extension settings first."

        units = ctx.settings.get("units") or "metric"   # 每次读最新值
        proxy = ctx.proxy_url()
        handlers = [urllib.request.ProxyHandler({"https": proxy})] if proxy else []
        opener = urllib.request.build_opener(*handlers)
        url = f"{endpoint}?{urllib.parse.urlencode({'city': city, 'units': units})}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener.open(req, timeout=10) as resp:
                return json.dumps(json.load(resp), ensure_ascii=False)
        except OSError as exc:
            ctx.logger.warning("forecast failed: %s", exc)  # 不要把 proxy 打进日志
            return f"Error: could not reach the weather service ({exc})."

    ctx.register_tool(
        "weather_forecast",
        forecast,
        description="Get the weather forecast for a city. Returns JSON.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    )

    def ask_city(call: ActionCall) -> ActionResult:
        city = call.values["city"]
        # prompt 会作为用户的下一条消息发出
        return ActionResult(message=f"Asking about {city}…", prompt=f"What's the weather in {city}?")

    ctx.register_action("ask_city", ask_city)
```

装好后：

1. 在扩展卡片上点 **Settings**，填 API token（保存后界面只显示“已设置”）。
2. 在输入框点 **Extension ▾ → Weather**，左下角出现 Weather 标签和 **Ask about a city** 按钮。
3. 点按钮、输入城市、提交，应用会替你发出一条“What's the weather in …?”的消息，模型会调用 `weather_forecast`。

---

## 8. 技能的写法要点

技能是写给**模型**看的说明书，和普通的 `SKILL.md` 一样：

- 文件头必须有 `name` 和 `description`。`description` 要写“**什么时候用**”，模型靠它判断要不要读正文。
- 正文写具体步骤、注意事项、命令示例。越具体，模型做得越稳。
- 技能可以引用同目录下的其它文件（脚本、模板），这些文件只在技能被执行时才会用到。
- 想让技能默认可用，在清单 `skills.default_modes` 里设置；用户随时可以在技能页改。
- 一个只带技能的扩展**不需要 `entry`**，不运行任何代码，是最安全的扩展形式。

---

## 9. 自带前端面板（高级）

清单声明按钮和对话框，足够应对大多数场景。如果你需要**自己画界面**（列表、上传、多步骤流程），可以带一个前端模块。

### 9.1 清单

```json
"ui": { "label": "My Panel", "icon": "🧩", "module": "web/index.js" }
```

- `module` 必须在**子目录**里（如 `web/index.js`），后缀是 `.js` 或 `.mjs`。**只有这个子目录会被浏览器访问**，你的 Python 和配置不会被暴露。
- 能被访问的文件类型有白名单：`.js` `.mjs` `.css` `.json` `.svg` `.png` `.jpg` `.jpeg` `.gif` `.webp` `.ico` `.woff2` `.txt` `.map`；单个文件最大 5 MB。
- 前端模块里的相对 `import` 可以用（`import "./util.js"`）。

### 9.2 模块写法

模块是原生 ES 模块，导出 `activate(api)`：

```js
// web/index.js
export function activate(api) {
  const { h, ref, onMounted } = api.vue;     // 用应用自己的 Vue

  api.panel({
    // 按钮名：可以是固定字符串，也可以按语言给
    title: { en: "Settings", "zh-CN": "设置", "zh-TW": "設定" },
    component: {
      setup() {
        const items = ref([]);
        onMounted(async () => {
          items.value = (await api.fetch("status")).items;   // GET /api/ext/<name>/status
        });
        return () =>
          h("div", [
            h("ul", items.value.map((x) => h("li", { key: x }, x))),
            h("button", { type: "button", onClick: () => api.toast.success("Hi") }, "Say hi"),
          ]);
      },
    },
  });
}
```

上面的 `api.fetch("status")` 请求的是本扩展自己的路由，所以清单和入口代码里也要有对应的声明和处理函数（§4.8、§6.6）：

```json
"routes": [ { "method": "GET", "path": "status" } ]
```

```python
ctx.register_route("GET", "status", lambda req: {"items": ["alpha", "beta"]})
```

规则（违反会导致模块加载失败，原因显示在界面上）：

- **不要** `import ... from "vue"`：浏览器里裸模块名解析不了，而且会产生第二个 Vue 实例，破坏响应式。**只用 `api.vue`**。
- **不能用模板**（没有模板编译器），用渲染函数 `h(...)`。
- `api.vue` 提供：`h` `ref` `shallowRef` `reactive` `computed` `watch` `watchEffect` `onMounted` `onUnmounted` `nextTick` `defineComponent` `Fragment` `Teleport` `Transition` `withModifiers` `withKeys`。

### 9.3 `api` 接口

| 成员 | 说明 |
|---|---|
| `api.name` | 扩展名 |
| `api.locale` | 当前界面语言：`en` / `zh-CN` / `zh-TW`，**实时读取**（切换语言后会变，在渲染函数里读它，界面会自动更新） |
| `api.vue` | 应用自己的 Vue（见上） |
| `api.fetch(path, {method, body})` | 请求**本扩展自己**的路由 `/api/ext/<name>/<path>`，登录和 CSRF 由应用处理；`path` 不能包含 `..`。返回解析后的 JSON |
| `api.upload(path, file)` | 把一个 `File`/`Blob` 作为**原始字节**以 POST 发给本扩展的路由（对应路由要在清单里调大 `max_body_mb`） |
| `api.mcpStatus()` | 读取全局 MCP 状态 |
| `api.setMcpEnabled(on)` | 开关全局 MCP 总开关 |
| `api.settings.get()` | 读取用户设置的**非密码**值 |
| `api.panel({title, component})` | 注册面板：选中扩展后，标签右边出现一个以 `title` 命名的按钮，点开是弹窗，里面渲染 `component`。`title` 可以是字符串，也可以是 `{ en, "zh-CN", "zh-TW" }` |
| `api.toast.success(text)` / `.error(text)` | 提示条 |
| `api.sendPrompt(text)` | 把 `text` 作为用户的下一条聊天消息发出 |
| `api.onDispose(fn)` | 扩展被卸载或重载时运行 `fn` |

**关于按钮名**：扩展名已经显示在左下角的标签上了，按钮表达“用途”即可，**建议叫“设置 / Settings”**，不要重复写扩展名。

### 9.4 多语言

界面文字请按 `api.locale` 切换，例如：

```js
const isEnglish = computed(() => String(api.locale || "zh-CN").startsWith("en"));
const tr = (zh, en) => (isEnglish.value ? en : zh);
// 渲染函数里： h("h3", tr("安装预览", "Installation preview"))
```

在渲染函数里读 `isEnglish.value`，语言切换时界面会自动重画。

### 9.5 重载

浏览器无法卸载一个 ES 模块。重载扩展时，应用用新的 `?v=` 地址加载新版本、运行旧版本的 `onDispose`、丢掉旧版本登记的东西；旧代码仍留在内存里，**刷新页面**即可彻底清掉（只在开发扩展时会注意到）。

---

## 10. 分发：打成 zip

```
hello-ext.zip
└── hello-ext/            ← 可以有这一层文件夹，也可以直接把 extension.json 放在 zip 根
    ├── extension.json
    └── main.py
```

- 在设置页 **Add .zip…** 选择它即可。
- zip 里 `extension.json` 要么在**根目录**，要么在**唯一的一层顶层文件夹**里；有多个顶层文件夹会被拒绝。
- 压缩包的限制：上传的 zip 本身最大 100 MB；最多 5000 个文件，解压后最大 200 MB；不允许绝对路径、`..`、盘符、符号链接。
- “Add folder…” 的限制相同；`__pycache__` 和 `.git` 会被跳过。
- **同名扩展已安装时**，确认框会提示“将替换已安装的版本”，需要你确认；**内置扩展不能被同名扩展覆盖**。
- 删除扩展时，可以勾选“同时删除数据”，这会删掉 `ctx.data_dir`、已保存的设置和密钥。不勾选则保留，重装后还在。

---

## 11. 出错了怎么办

| 现象 | 原因 / 办法 |
|---|---|
| 卡片显示 **Failed** 和一行原因 | 按原因修改后点 **Reload**。清单字段错误会指明是哪一项 |
| `unknown field(s) 'xxx'` | 清单里有拼错或不支持的字段 |
| `name 'xxx' must equal its directory name` | 文件夹名和清单 `name` 不一致 |
| `tools requires entry` | 声明了工具但没有 `entry` |
| `register_tool('x'): not declared in the manifest's tools` | 代码注册了清单里没有声明的工具；两边都要写 |
| 工具没出现，卡片上有警告 `refused: the name is already used` | 工具名和别的工具重名，换个名字 |
| 技能没出现，警告 `skill 'x' refused` | 技能 ID 已被占用，改文件夹名 |
| `the entry defines no activate(ctx) function` | 入口里没有 `activate`，或者写成了 `async def` |
| 页面只显示 “HTTP 400 returned a non-conforming error body” | 路由错误没有按 §6.6 的格式返回 |
| 改了代码没生效 | 点该扩展的 **Reload**；前端模块的改动还要刷新页面 |

**日志**：用 `ctx.logger` 写的内容和应用错误一起写进应用日志，日志名是 `qai.user_extension.<扩展名>`。

---

## 12. 开发小技巧

- **先写清单，再写代码。** 清单是契约：声明了才能注册，没声明的注册会失败。
- **从小做起。** 先做出只有一个工具的扩展装进去，再逐项加技能、设置、按钮。
- **普通函数就够了。** 除非你要 `await` 别的异步库，否则用普通函数，应用会替你放进工作线程。
- **别在 `activate` 里做耗时的事。** 加载扩展会等它返回。联网、读大文件放到处理函数里。
- **写入只放 `ctx.data_dir`。** 扩展目录在升级时会被整个替换。
- **密码只放 `secret`。** 不要写进代码、`config.toml`、日志。
- **工具名要有辨识度。** 带上你的领域前缀，避免和别的扩展重名。
- **`group` 如实填。** 会改东西的工具别标成 `read`。

---

## 13. 速查表

**清单最小模板**

```json
{
  "manifest_version": 1,
  "name": "my-ext",
  "version": "1.0.0",
  "description": "What it does"
}
```

**入口最小模板**

```python
def activate(ctx):
    def handler(call):
        return "ok"

    ctx.register_tool("my_tool", handler, description="Does something.")
```

**`ctx` 速查**

| 成员 | 用途 |
|---|---|
| `name` `version` `root` | 身份与位置 |
| `data_dir` | 可写数据目录 |
| `config` | `config.toml` 只读默认值 |
| `settings` | 用户设置（非密码，实时） |
| `secrets.get/set/delete/keys` | 密钥 |
| `logger` | 日志 |
| `proxy_url()` `ssl_verify` | 网络环境 |
| `register_tool` | 注册工具 |
| `register_action` | 注册按钮处理函数 |
| `register_route` | 注册 HTTP 路由 |
| `register_mcp_server` | 运行时添加 MCP 服务器 |
| `on_unload` | 卸载时清理 |

**一个内置的完整参考**：应用自带的 `Qualcomm Skills` 扩展（`factory/extensions/qualcomm-skills/`）用到了清单、Python 入口、HTTP 路由、动态 MCP 服务器、前端面板、提示词和多语言，可以对照阅读。

**一个更小、更容易读的参考**：内置的「本机信息」扩展（`factory/extensions/system-info/`）——一个只读工具、两个不弹窗的按钮、一段模式提示词、三语言的名称，没有前端模块，适合作为第一个要读的例子。
