# Feishu channel: how it works

QAI ModelBuilder connects to Feishu (Lark) via a **custom self-built app** on the Feishu Open Platform. Bot events (user DMs, @ mentions, group messages) are routed to whichever AI model you pick in this panel. The **first connection** requires you to create the app on the Open Platform and paste four credentials into this panel; after that the credentials are stored locally and the channel auto-reconnects on restart.

## Connection flow

![Feishu connection flow](/help-images/feishu-setup/connection-flow.svg)

1. Create a **custom app** on the [Feishu Open Platform](https://open.feishu.cn/app).
2. Copy the **App ID** and **App Secret** from the app-management page (the App Secret is shown in plaintext only once, at creation).
3. Under **Event Subscriptions**, choose "WebSocket long connection" mode and generate the **Encrypt Key** and **Verification Token**.
4. Paste all four credentials into this panel and click **Save**.
5. Click **Connect** — the app opens a WebSocket long-connection to the Feishu event gateway.
6. Once the status flips to "connected", incoming Feishu messages flow to your selected AI model.

## Why WebSocket long-connection?

Feishu's Open Platform supports both "HTTP webhook callbacks" (which require a public IP) and "WebSocket long-connection" (which does not). QAI ModelBuilder typically runs on a developer's **local machine** without a stable public IP. The WebSocket mode is client-initiated — your machine dials out to the Feishu gateway and messages are pushed down that connection — so **you do not expose any inbound endpoint**.

## Common issues

- **`Invalid App Secret`**: pasting the App Secret from the Open Platform often carries invisible trailing whitespace. Re-copy it and clear the target field (Ctrl+A then Delete) before pasting.
- **`Signature mismatch`**: the Encrypt Key and Verification Token are swapped between fields. Go back to "Event Subscriptions → Encryption" and match each Open Platform value to the correct field here.
- **Connected but no messages**: the app is missing `im:message` / `im:message:send_as_bot` permissions, or the **app version has not been published**. Add the scopes on the Open Platform → create a new version → publish → disconnect and reconnect here.
- **Connection timeout**: your corporate network requires a proxy. Expand the **Proxy Settings** section at the bottom of this panel and fill in the HTTPS proxy URL.
- **Want to pause the bot**: click **Disconnect** on the panel. Local credentials are preserved — a subsequent click on Connect restores the session without re-onboarding.

## Official references

- Feishu Open Platform: <https://open.feishu.cn/app>
- Event subscription docs: <https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/event-subscription-guide>
