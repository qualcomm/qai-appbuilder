---
name: Email 163
description: 163 email tool for sending, reading, searching, and replying to emails via 163.com (NetEase Mail).
tags: email, 163, netease, mail
use_for: Sending emails, reading emails, searching emails, replying to emails via 163.com
---

## CRITICAL: You MUST call the exec tool

When this skill is active, you MUST immediately call the exec tool based on the user's request. Do NOT output code blocks or describe steps.

## 1. Read Emails

Read the latest 5 emails:
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/main.py read"}}
</tool_call>

Read a specific number of emails:
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/main.py read --count 10"}}
</tool_call>

Read a full email (including body):
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/main.py read --id 123"}}
</tool_call>

## 2. Send Email

Simple send:
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/main.py send --to friend@example.com --subject \"Hello\" --body \"Hi!\""}}
</tool_call>

## 3. Reply to Email

Reply to a specific email (use the original sender's address as recipient, prefix subject with "Re:"):
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/main.py send --to <sender_email_address> --subject \"Re: <original_subject>\" --body \"<reply_body>\""}}
</tool_call>
