# WeChat channel: how iLink Bot works

QAI ModelBuilder connects to your personal WeChat account via **iLink Bot** — a third-party WebSocket relay — not through any official WeChat SDK. The first connection needs one **QR-code authorisation**; after that the credential is cached locally and the channel reconnects automatically on restart.

## Authorisation sequence

![iLink Bot authorisation sequence](/help-images/wechat-ilink/authorization-flow.svg)

1. You click **Connect WeChat** in the settings panel.
2. QAI ModelBuilder opens iLink Bot's authorisation page.
3. You scan the QR code inside the WeChat mobile app.
4. iLink Bot prompts "Authorise this bot?".
5. You tap **Confirm**.
6. iLink Bot delivers a **long-lived session token** back to QAI ModelBuilder via webhook.
7. The panel flips to "Connected". Incoming WeChat messages are then routed to whatever model you configured.

## Why go through iLink at all?

WeChat has no official bot API for personal accounts. iLink Bot hosts the WebSocket long-connection that turns your DMs into events our service can consume. **Neither your account credentials nor your chat history transit any QAI server** — iLink is the sole intermediary.

## Common issues

- **QR expired** — default life is 3 minutes. Click "Refresh QR" and rescan.
- **Nothing happens after scanning** — make sure the WeChat app is running in the foreground and not aggressively battery-managed, then rescan.
- **Autoreconnect fails after a drop** — usually iLink servers being unreachable. Check the error detail on the panel; if it persists beyond ~5 min, check iLink's status page.
- **Want to unbind?** — click **Disconnect** on the panel; the local token is invalidated immediately.

## Official reference

- iLink Bot: <https://ilink.dev/>
