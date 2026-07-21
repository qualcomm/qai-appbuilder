# Download settings and network troubleshooting

The Download Centre pulls model archives from a remote catalogue to a local directory on your machine. It uses `aria2c` (multi-threaded, resumable) by default and reads the list of available models from a "version list URL".

## Fields you actually change

- **Save Dir** — where extracted models land. **Prefer paths without spaces, non-ASCII, or special characters**. QNN runtime silently fails to load a few plugins from non-ASCII paths. The default `QAIModelBuilder/downloads/` is safe.
- **Version list URL** — JSON manifest of releases (default: project GitHub release). Point this at your private mirror if you host one.
- **Catalog URL** — JSON with the catalogue of downloadable models.
- **fetch_timeout / download_timeout** — seconds for "fetch the manifest" and "download one file" respectively.
- **SSL verify** — leave ON in production. Turn off only temporarily inside an internal CA environment.

## When a download fails

1. **Corporate proxy required** — fill the HTTP/SOCKS proxy in the Proxy section below; it is inherited by the `aria2c` child process.
2. **Timeout** — raise `fetch_timeout_seconds` from 30 to 120. Common the first time you pull a new model from a slow mirror.
3. **`aria2c: command not found`** — the Windows build usually bundles `aria2c.exe`; reinstall or grab a binary from <https://github.com/aria2/aria2/releases>.
4. **Half-downloaded** — `aria2c` resumes. Just **retry the same model**; completed segments are kept.
5. **SSL error** — check whether a corporate MITM proxy is intercepting TLS. **Do not** disable SSL verify permanently.

## Official references

- aria2 project: <https://aria2.github.io/>
- aria2 CLI manual: <https://aria2.github.io/manual/en/html/aria2c.html>
