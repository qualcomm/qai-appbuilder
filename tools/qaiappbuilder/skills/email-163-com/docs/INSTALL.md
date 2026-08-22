# email-163-com Configuration Guide

## Prerequisites

- Python 3.6+ (uses only the standard library; no extra dependencies required)
- A 163 Mail account with IMAP/SMTP service enabled

## Step 1: Enable IMAP/SMTP Service for 163 Mail

1. Log in to [mail.163.com](https://mail.163.com)
2. Go to **Settings → POP3/SMTP/IMAP**
3. Enable the **IMAP/SMTP service**
4. Generate and record the **client authorization code** (note: this is NOT your login password)

## Step 2: Create the Configuration File

Configuration file path: `~/.config/email-163-com/config.json`

Manually create the directory and file:

```bash
mkdir -p ~/.config/email-163-com
```

Write the following content (replace the `email` and `password` fields):

```json
{
  "email": "your_email@163.com",
  "password": "your_imap_auth_code",
  "imap_server": "imap.163.com",
  "imap_port": 993,
  "smtp_server": "smtp.163.com",
  "smtp_port": 465,
  "imap_id": {
    "name": "OpenClaw",
    "version": "1.0.0",
    "vendor": "email-163-com",
    "support_email": "your_email@163.com"
  },
  "defaults": {
    "folder": "INBOX",
    "count": 5,
    "output_dir": "~/Downloads"
  }
}
```

**Field descriptions:**

| Field | Description |
|------|------|
| `email` | 163 Mail address |
| `password` | Client authorization code (not the login password) |
| `imap_server` / `imap_port` | IMAP server, fixed at `imap.163.com:993` |
| `smtp_server` / `smtp_port` | SMTP server, fixed at `smtp.163.com:465` |
| `imap_id.name` | Client identifier name (required by 163 Mail; default is fine) |
| `defaults.count` | Default number of emails to read |
| `defaults.output_dir` | Default download directory for attachments |

## Step 3: Use the Interactive Wizard (Optional)

You can also auto-create the configuration file via the built-in wizard:

```bash
python main.py init
```

The wizard prompts you in turn for the email address and authorization code, then
automatically tests the connection once complete.

## Step 4: Verify the Configuration

```bash
python main.py read --count 5
```

If an email list is printed, the configuration succeeded.

## FAQ

**Authentication failure / Unsafe Login error**
- Confirm you are using the **authorization code**, not the login password
- Regenerate the authorization code and update the configuration file

**IMAP service not enabled**
- Log in to the 163 Mail web client and confirm the IMAP/SMTP service is enabled

**SSL connection failure**
- Check your network connection and firewall, and make sure ports 993/465 are reachable
