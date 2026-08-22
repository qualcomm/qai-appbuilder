# 下载设置与网络排查

下载中心用于把模型压缩包从远端拉到你电脑上的本地目录。默认走 `aria2c` 多线程下载（大文件更快、支持断点续传），从"版本清单 URL"读取可用模型列表。

## 常用字段

- **保存目录（Save Dir）**：模型解压后放到哪里。**尽量避免路径里出现空格 / 中文 / 特殊字符**——QNN runtime 加载有些插件时会因非 ASCII 路径静默失败。默认值 `QAIModelBuilder/downloads/` 是安全的。
- **Version list URL**：版本清单的 JSON 地址（默认指向本项目 GitHub Release）。私有镜像时可以改成自建的 HTTPS/HTTP 地址。
- **Catalog URL**：目录（可下载模型列表）JSON 地址。
- **fetch_timeout / download_timeout**：分别控制"抓清单"和"下载单个文件"的超时秒数。
- **SSL verify**：默认开启。仅在内部签发证书环境下临时关闭；线上环境不要关闭。

## 遇到下载失败？

1. **公司网需要代理**：在此面板下方的"代理设置"填 HTTP/SOCKS 代理地址；同一代理会传给 `aria2c` 子进程。
2. **超时**：把 `fetch_timeout_seconds` 从默认 30 提到 120。首次下载新模型且服务器较慢时常见。
3. **`aria2c: command not found`**：Windows 版通常已捆绑 `aria2c.exe`；若丢失，重新安装或从 <https://github.com/aria2/aria2/releases> 补装。
4. **只下了一半**：`aria2c` 支持断点续传——直接**重试同一模型**即可续下，不会重新下载已完成的分段。
5. **SSL 报错**：先检查是否走了公司 MITM 代理。**不要**把 SSL verify 永久关掉。

## 官方参考

- aria2 官网：<https://aria2.github.io/>
- aria2 命令行手册：<https://aria2.github.io/manual/en/html/aria2c.html>
