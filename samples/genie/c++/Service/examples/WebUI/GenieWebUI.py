# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import time
import os
import threading
import shutil
import re
import requests
from PIL import Image
import gradio as gr
from gradio import ChatMessage
from chat import Chat
from Docutils import ManualDocSummarizer
import base64
import webbrowser
import http.server
import socketserver

os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

# ===================================================================
# 🌐 前端 UI 层（仅保留结构，后端调用将通过 HTTP）
# 所有实际逻辑用 pass 替代，留出接口接入点
# ===================================================================
service = Chat()
docx_summary = ManualDocSummarizer()

current_model = ""
HOST = "0.0.0.0"
#HOST = "127.0.0.1"
PORT = 50000
FILE_PATH = "files"
TRUSTED_OUTPUT_DIR="images"
AUDIO_TYPES = ['.wav']
FILE_TYPES = [".pdf", ".docx", ".pptx", ".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp"]
IMG_TYPES = [".png", ".jpg", "jpeg"]
FUNC_LIST = ["📐 解题答疑", "📚 文档总结", "🗛 AI 翻 译", "🌐 AI 搜 索", "✒️ 帮我写作", "🎨 图像生成", "🍸 定制功能", "✈️ 旅游规划"]
FUNC_LIST_EN = ["📐 Q & A", "📚 Doc Summary", "🗛 AI Translation", "🌐 AI Searching", "✒️ Writing Assistant", "🎨 Text To Image", "🍸 Customerized Function", "✈️ Tourism planning"]

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    END = '\033[0m'


###########################################################################

css="""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');

/* ── CSS Custom Properties ────────────────────────────────────── */
:root {
    --bg-base:        #080808;
    --bg-panel:       rgba(12,12,14,0.97);
    --bg-sidebar:     rgba(255,255,255,0.018);
    --bg-input:       rgba(16,16,18,0.98);
    --bg-button:      rgba(255,255,255,0.03);
    --bg-msg-user:    linear-gradient(135deg, rgba(245,158,11,0.12) 0%, rgba(251,191,36,0.07) 100%);
    --bg-msg-bot:     rgba(255,255,255,0.025);
    --bg-chatbot:     rgba(8,8,8,0.5);
    --border-main:    rgba(245,158,11,0.12);
    --border-sidebar: rgba(245,158,11,0.07);
    --border-input:   rgba(245,158,11,0.2);
    --border-msg-user:rgba(245,158,11,0.28);
    --border-msg-bot: rgba(255,255,255,0.06);
    --accent-1:       #f59e0b;
    --accent-2:       #06b6d4;
    --accent-3:       #f43f5e;
    --text-primary:   #f0ece4;
    --text-secondary: #c8c4bc;
    --text-muted:     rgba(200,196,188,0.45);
    --text-label:     rgba(245,158,11,0.55);
    --header-bg:      #080808;
    --header-border:  rgba(245,158,11,0.1);
    --btn-grad:       linear-gradient(135deg, #d97706, #f59e0b);
    --scrollbar:      rgba(245,158,11,0.18);
    --chat-panel-bg:  #080808;
    --section-title:  rgba(245,158,11,0.4);
    --dot-color:      #06b6d4;
    --shadow-dark:    rgba(0,0,0,0.6);
    --inset-shine:    rgba(255,255,255,0.025);
    --noise-opacity:  0.025;
    --font-display:   'Syne', sans-serif;
    --font-mono:      'JetBrains Mono', monospace;
    --font-body:      'DM Sans', sans-serif;
}

/* ── Reset & Base ─────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
    background: var(--bg-base) !important;
    min-height: 100%;
    font-family: var(--font-body);
    color: var(--text-primary);
}

/* Noise texture overlay on body */
body::before {
    content: '';
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='1'/%3E%3C/svg%3E");
    opacity: var(--noise-opacity);
}

/* ── Scrollbar ────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
    border-radius: 99px;
    background: var(--scrollbar);
}
::-webkit-scrollbar-thumb:hover { background: var(--accent-1); }

/* ── Hide Gradio chrome ───────────────────────────────────────── */
footer { display: none !important; }
.block { border: none !important; box-shadow: none !important; background: transparent !important; }

/* ── App shell ────────────────────────────────────────────────── */
.gradio-container {
    background: var(--bg-base) !important;
    max-width: 100% !important;
    width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}

/* ── Sidebar section dividers ─────────────────────────────────── */
.sidebar-section-title {
    font-family: var(--font-mono);
    font-size: 0.58rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 2.5px;
    color: var(--section-title);
    padding: 16px 2px 7px;
    border-bottom: 1px solid var(--border-sidebar);
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 7px;
    position: relative;
}
.sidebar-section-title::before {
    content: '//';
    font-family: var(--font-mono);
    font-size: 0.55rem;
    color: var(--accent-1);
    opacity: 0.6;
    margin-right: 2px;
}
.sidebar-section-title:first-child { padding-top: 2px; }

/* ── All Gradio inputs / textareas / selects — theme-aware ──── */

/* Generic text inputs and textareas */
.gradio-container input[type=text],
.gradio-container input[type=number],
.gradio-container input[type=search],
.gradio-container input[type=email],
.gradio-container input[type=password],
.gradio-container textarea {
    background: var(--bg-input) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 4px !important;
    color: var(--text-primary) !important;
    font-family: var(--font-mono) !important;
    font-size: 0.82rem !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
.gradio-container input[type=text]:focus,
.gradio-container input[type=number]:focus,
.gradio-container input[type=search]:focus,
.gradio-container textarea:focus {
    border-color: var(--accent-1) !important;
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent-1) 10%, transparent),
                0 0 12px color-mix(in srgb, var(--accent-1) 8%, transparent) !important;
    outline: none !important;
}
.gradio-container input::placeholder,
.gradio-container textarea::placeholder {
    color: var(--text-muted) !important;
    font-style: italic !important;
}

/* Gradio label text */
.gradio-container label span,
.gradio-container .label-wrap span {
    color: var(--text-label) !important;
    font-family: var(--font-mono) !important;
    font-size: 0.68rem !important;
    font-weight: 500 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
}

/* Gradio block / panel wrappers that hold inputs */
.gradio-container .block.padded,
.gradio-container .form {
    background: transparent !important;
    border: none !important;
}

/* Sliders */
input[type=range] {
    accent-color: var(--accent-1);
    width: 100%;
}
/* Slider number input beside the track */
.gradio-container .wrap.svelte-p2hrgn input,
.gradio-container span.svelte-p2hrgn input {
    background: var(--bg-input) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 3px !important;
    color: var(--text-primary) !important;
    font-family: var(--font-mono) !important;
}

/* Native <select> and Gradio dropdown */
.gradio-container select,
.gr-dropdown select {
    background: var(--bg-input) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 4px !important;
    color: var(--text-primary) !important;
    font-family: var(--font-mono) !important;
    font-size: 0.82rem !important;
    padding: 6px 10px !important;
    transition: border-color 0.2s ease !important;
}
.gradio-container select:focus,
.gr-dropdown select:focus {
    border-color: var(--accent-1) !important;
    outline: none !important;
}

/* Gradio Dropdown component (svelte-select) — theme-aware */
.gradio-container .svelte-select,
.gradio-container [class*="svelte-select"],
.gradio-container .wrap > .wrap,
.gradio-container .input-wrap,
.gradio-container .value-container {
    background: var(--bg-input) !important;
    color: var(--text-primary) !important;
    border-color: var(--border-input) !important;
}
.gradio-container .svelte-select-list,
.gradio-container [class*="svelte-select-list"],
.gradio-container ul[role=listbox] {
    background: var(--bg-panel) !important;
    border: 1px solid var(--border-input) !important;
    color: var(--text-secondary) !important;
}
.gradio-container .svelte-select-list .item,
.gradio-container [class*="svelte-select-list"] [class*="item"],
.gradio-container li[role=option] {
    background: transparent !important;
    color: var(--text-secondary) !important;
}
.gradio-container .svelte-select-list .item:hover,
.gradio-container .svelte-select-list .item.active,
.gradio-container li[role=option]:hover,
.gradio-container li[role=option][aria-selected=true] {
    background: color-mix(in srgb, var(--accent-1) 15%, transparent) !important;
    color: var(--text-primary) !important;
}
/* Dropdown selected value text */
.gradio-container .selection,
.gradio-container .selected-item,
.gradio-container [class*="selected-item"] {
    color: var(--text-primary) !important;
    background: transparent !important;
}

/* Gradio Textbox component wrapper */
.gradio-container .svelte-1f354aw,
.gradio-container [class*="textbox"] > .wrap {
    background: var(--bg-input) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 10px !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
.gradio-container .svelte-1f354aw:focus-within,
.gradio-container [class*="textbox"] > .wrap:focus-within {
    border-color: var(--accent-1) !important;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-1) 12%, transparent) !important;
}

/* ── Metric cards ─────────────────────────────────────────────── */
.metric-box input,
.metric-box textarea {
    background: rgba(245,158,11,0.04) !important;
    border: 1px solid rgba(245,158,11,0.15) !important;
    border-radius: 3px !important;
    color: var(--accent-1) !important;
    font-family: var(--font-mono) !important;
    font-weight: 500 !important;
    font-size: 0.82rem !important;
    text-align: center !important;
    font-variant-numeric: tabular-nums !important;
    letter-spacing: 0.5px !important;
}

/* ── Header ───────────────────────────────────────────────────── */
#genie-header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 16px 28px 14px;
    background: var(--header-bg);
    border-bottom: 1px solid var(--header-border);
    position: relative;
    overflow: hidden;
}
/* Scanline effect on header */
#genie-header::after {
    content: '';
    position: absolute;
    inset: 0;
    background: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(245,158,11,0.012) 2px,
        rgba(245,158,11,0.012) 4px
    );
    pointer-events: none;
}

/* ── Tab nav ──────────────────────────────────────────────────── */
.tab-nav {
    background: transparent !important;
    border-bottom: 1px solid var(--border-sidebar) !important;
    border-radius: 0 !important;
    border-top: none !important;
    border-left: none !important;
    border-right: none !important;
    padding: 0 20px !important;
    gap: 0 !important;
}
.tab-nav button {
    border-radius: 0 !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    letter-spacing: 0.2px !important;
    color: var(--text-muted) !important;
    transition: all 0.2s ease !important;
    padding: 12px 18px !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    margin-bottom: -1px !important;
    background: transparent !important;
}
.tab-nav button.selected {
    color: var(--accent-1) !important;
    border-bottom-color: var(--accent-1) !important;
    background: transparent !important;
    box-shadow: none !important;
}
.tab-nav button:hover:not(.selected) {
    color: var(--text-secondary) !important;
    background: var(--bg-sidebar) !important;
}

/* ── Message bubbles ──────────────────────────────────────────── */
/* Row alignment: user right, bot left */
.message.user {
    display: flex !important;
    flex-direction: row !important;
    justify-content: flex-end !important;
    align-items: flex-start !important;
    width: 100% !important;
}
.message.bot {
    display: flex !important;
    flex-direction: row !important;
    justify-content: flex-start !important;
    align-items: flex-start !important;
    width: 100% !important;
}
/* User bubble — fills the full available width of the chat panel */
.message.user .bubble-wrap,
.message.user > div:not(.avatar-container) {
    background: var(--bg-msg-user) !important;
    border: 1px solid var(--border-msg-user) !important;
    border-radius: 18px 18px 4px 18px !important;
    padding: 11px 16px !important;
    box-shadow: 0 2px 16px color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    color: var(--text-primary) !important;
    font-size: 0.9rem !important;
    line-height: 1.65 !important;
    width: 100% !important;
    display: block !important;
    word-break: break-word !important;
    white-space: normal !important;
    box-sizing: border-box !important;
}
/* Bot bubble — fills the full available width of the chat panel */
.message.bot .bubble-wrap,
.message.bot > div:not(.avatar-container) {
    background: var(--bg-msg-bot) !important;
    border: 1px solid var(--border-msg-bot) !important;
    border-radius: 4px 18px 18px 18px !important;
    padding: 11px 16px !important;
    box-shadow: 0 2px 12px rgba(0,0,0,0.25) !important;
    color: var(--text-secondary) !important;
    font-size: 0.9rem !important;
    line-height: 1.65 !important;
    width: 100% !important;
    display: block !important;
    word-break: break-word !important;
    white-space: normal !important;
    box-sizing: border-box !important;
}
/* All descendant divs/spans/p inside user and bot bubbles — collapse block wrappers */
.message.user > div:not(.avatar-container) *,
.message.bot > div:not(.avatar-container) * {
    display: inline !important;
    margin: 0 !important;
    padding: 0 !important;
}
/* Restore block display for actual multi-line content elements in both user and bot bubbles */
.message.user > div:not(.avatar-container) p,
.message.bot > div:not(.avatar-container) p {
    display: block !important;
    margin: 0 0 0.5em 0 !important;
    padding: 0 !important;
}
.message.user > div:not(.avatar-container) pre,
.message.user > div:not(.avatar-container) code,
.message.user > div:not(.avatar-container) ul,
.message.user > div:not(.avatar-container) ol,
.message.user > div:not(.avatar-container) li,
.message.user > div:not(.avatar-container) blockquote,
.message.user > div:not(.avatar-container) table,
.message.bot > div:not(.avatar-container) pre,
.message.bot > div:not(.avatar-container) code,
.message.bot > div:not(.avatar-container) ul,
.message.bot > div:not(.avatar-container) ol,
.message.bot > div:not(.avatar-container) li,
.message.bot > div:not(.avatar-container) blockquote,
.message.bot > div:not(.avatar-container) table {
    display: block !important;
    margin: 4px 0 !important;
    padding: revert !important;
}

/* ── Chatbot container: fill column + strip ALL borders/backgrounds ── */
/* Target every possible wrapper Gradio may generate around the chatbot */
.chatbot-wrap,
div[data-testid="chatbot"],
[class*="chatbot"] {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;
    padding: 0 !important;
    width: 100% !important;
    box-sizing: border-box !important;
}
/* Strip ALL child wrappers inside the chatbot that are NOT message bubbles */
.chatbot-wrap > div:not(.message),
div[data-testid="chatbot"] > div:not(.message),
[class*="chatbot"] > div:not(.message),
.chatbot-wrap > div:not(.message) > div:not(.message),
div[data-testid="chatbot"] > div:not(.message) > div:not(.message) {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;
    padding: 0 !important;
    width: 100% !important;
    box-sizing: border-box !important;
}
/* Strip the chat column wrapper itself */
#chat-col,
#chat-col > .block,
#chat-col > .wrap,
#chat-col > div {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;
    padding: 0 !important;
}
/* Restore padding/background/border on the actual user message bubbles */
.message.user .bubble-wrap,
.message.user > div:not(.avatar-container) {
    padding: 11px 16px !important;
    background: var(--bg-msg-user) !important;
    border: 1px solid var(--border-msg-user) !important;
    box-shadow: 0 2px 16px color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    border-radius: 18px 18px 4px 18px !important;
    width: 100% !important;
    display: block !important;
    word-break: break-word !important;
    white-space: normal !important;
    box-sizing: border-box !important;
    color: var(--text-primary) !important;
    font-size: 0.9rem !important;
    line-height: 1.65 !important;
}
/* Restore padding/background/border on the actual bot message bubbles */
.message.bot .bubble-wrap,
.message.bot > div:not(.avatar-container) {
    padding: 11px 16px !important;
    background: var(--bg-msg-bot) !important;
    border: 1px solid var(--border-msg-bot) !important;
    box-shadow: 0 2px 12px rgba(0,0,0,0.25) !important;
    border-radius: 4px 18px 18px 18px !important;
    width: 100% !important;
    display: block !important;
    word-break: break-word !important;
    white-space: normal !important;
    box-sizing: border-box !important;
    color: var(--text-secondary) !important;
    font-size: 0.9rem !important;
    line-height: 1.65 !important;
}
/* The scrollable messages area inside the chatbot */
.chatbot-wrap .overflow-y-auto,
div[data-testid="chatbot"] .overflow-y-auto,
.chatbot-wrap [class*="scroll"],
div[data-testid="chatbot"] [class*="scroll"] {
    width: 100% !important;
    box-sizing: border-box !important;
    overflow-y: auto !important;
}
/* Each message row fills the full width — strip any outer rectangular frame */
.message {
    width: 100% !important;
    box-sizing: border-box !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;
    padding: 2px 0 !important;
}
/* Also strip any Gradio-generated wrapper divs directly inside the chatbot scroll area
   that wrap individual messages (one level above .message) */
.chatbot-wrap .overflow-y-auto > div,
div[data-testid="chatbot"] .overflow-y-auto > div,
[class*="chatbot"] .overflow-y-auto > div {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;
    padding: 0 !important;
}

/* ── Hide chatbot label, like/unlike, and share icons ─────────── */
/* Hide the "Chatbot" label/title above the chat window (belt-and-suspenders with show_label=False) */
.chatbot-wrap .label-wrap,
.chatbot-wrap label,
div[data-testid="chatbot"] .label-wrap,
div[data-testid="chatbot"] label,
[class*="chatbot"] .label-wrap,
[class*="chatbot"] label,
.wrap > .label-wrap:first-child,
.block > .label-wrap:first-child {
    display: none !important;
}
/* Hide like / unlike (thumbs) buttons */
.message-buttons button[title="Like"],
.message-buttons button[title="Dislike"],
.message-buttons button[aria-label="Like"],
.message-buttons button[aria-label="Dislike"],
.message-buttons button[title="Good response"],
.message-buttons button[title="Bad response"],
button.like-button,
button.dislike-button,
[data-testid="like-button"],
[data-testid="dislike-button"] {
    display: none !important;
}
/* Hide share button */
.message-buttons button[title="Share"],
.message-buttons button[aria-label="Share"],
button.share-button,
[data-testid="share-button"],
.chatbot-wrap button[title="Share"],
div[data-testid="chatbot"] button[title="Share"] {
    display: none !important;
}
/* Hide the entire message-buttons row if it becomes empty */
.message-buttons:empty {
    display: none !important;
}
/* Broader catch-all: hide all action icon buttons in the chatbot header area */
div[data-testid="chatbot"] > div:first-child > button,
.chatbot-wrap > div:first-child > button,
/* Gradio 4.x chatbot action bar — share / download / clear buttons */
div[data-testid="chatbot"] > div > div > button,
div[data-testid="chatbot"] > div > button,
[class*="chatbot"] > div > button,
[class*="chatbot"] > div > div > button,
/* Any button directly inside the chatbot wrapper that is NOT inside a message */
div[data-testid="chatbot"] button:not(.message button):not([class*="message"] button) {
    display: none !important;
}
/* Hide copy button on every message */
.message-buttons button[title="Copy"],
.message-buttons button[aria-label="Copy"],
.message-buttons button[title="copy"],
[data-testid="copy-button"],
button.copy-button,
.message-buttons button svg[data-icon="copy"],
.message-buttons button:has(svg[data-icon="copy"]) {
    display: none !important;
}
/* Hide the entire message-buttons toolbar if all buttons are hidden */
.message-buttons {
    display: none !important;
}

/* ── Avatar containers ────────────────────────────────────────── */
.message .avatar-container {
    flex-shrink: 0 !important;
    width: 38px !important;
    height: 38px !important;
    border-radius: 50% !important;
    overflow: hidden !important;
    border: 1.5px solid color-mix(in srgb, var(--accent-1) 30%, transparent) !important;
    box-shadow: 0 0 12px color-mix(in srgb, var(--accent-1) 22%, transparent) !important;
    background: var(--bg-panel) !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.message.user .avatar-container {
    margin-left: 10px !important;
    order: 2 !important;
}
.message.bot .avatar-container {
    margin-right: 10px !important;
    order: 0 !important;
}
.message .avatar-container img {
    width: 100% !important;
    height: 100% !important;
    object-fit: cover !important;
    border-radius: 50% !important;
    display: block !important;
}

/* ── Function mode buttons ────────────────────────────────────── */
.button_cls {
    background: var(--bg-button) !important;
    border: 1px solid color-mix(in srgb, var(--accent-1) 18%, transparent) !important;
    border-radius: 20px !important;
    color: var(--text-muted) !important;
    font-size: 0.72rem !important;
    font-weight: 600 !important;
    padding: 5px 13px !important;
    transition: all 0.18s ease !important;
    cursor: pointer !important;
    white-space: nowrap !important;
    letter-spacing: 0.2px !important;
    min-height: unset !important;
    height: auto !important;
    line-height: 1.4 !important;
}
.button_cls:hover {
    background: linear-gradient(135deg,
        color-mix(in srgb, var(--accent-1) 18%, transparent),
        color-mix(in srgb, var(--accent-2) 18%, transparent)) !important;
    border-color: color-mix(in srgb, var(--accent-1) 45%, transparent) !important;
    color: var(--accent-1) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 14px color-mix(in srgb, var(--accent-1) 14%, transparent) !important;
}
.button_cls:active { transform: translateY(0) !important; }

/* MultimodalTextbox — theme-aware, all internal elements */
#chatmsg-box,
#chatmsg-box > div,
#chatmsg-box > div > div,
.gr-multimodal-textbox,
[class*="multimodal"],
[class*="multimodal"] > div,
[class*="multimodal"] > div > div,
div[data-testid="multimodal-textbox"],
div[data-testid="multimodal-textbox"] > div,
div[data-testid="multimodal-textbox"] > div > div {
    background: var(--bg-input) !important;
    color: var(--text-primary) !important;
}
/* Outer border/shadow only on the top-level wrapper */
#chatmsg-box,
.gr-multimodal-textbox,
[class*="multimodal"],
div[data-testid="multimodal-textbox"] {
    border: 1.5px solid var(--border-input) !important;
    border-radius: 14px !important;
    transition: border-color 0.22s ease, box-shadow 0.22s ease !important;
    box-shadow: 0 4px 28px var(--shadow-dark), inset 0 1px 0 var(--inset-shine) !important;
}
#chatmsg-box:focus-within,
.gr-multimodal-textbox:focus-within,
[class*="multimodal"]:focus-within,
div[data-testid="multimodal-textbox"]:focus-within {
    border-color: var(--accent-1) !important;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-1) 12%, transparent),
                0 4px 28px var(--shadow-dark) !important;
}
/* Textarea inside */
#chatmsg-box textarea,
[class*="multimodal"] textarea,
div[data-testid="multimodal-textbox"] textarea {
    background: transparent !important;
    border: none !important;
    color: var(--text-primary) !important;
    font-size: 0.92rem !important;
    line-height: 1.6 !important;
    padding: 10px 14px !important;
    resize: none !important;
    box-shadow: none !important;
}
#chatmsg-box textarea::placeholder,
[class*="multimodal"] textarea::placeholder,
div[data-testid="multimodal-textbox"] textarea::placeholder {
    color: var(--text-muted) !important;
    font-style: italic !important;
}
/* Submit / send button */
#chatmsg-box button[aria-label="Submit"],
#chatmsg-box button[type=submit],
[class*="multimodal"] button[aria-label="Submit"],
[class*="multimodal"] button[type=submit],
div[data-testid="multimodal-textbox"] button[aria-label="Submit"],
div[data-testid="multimodal-textbox"] button[type=submit] {
    background: var(--btn-grad) !important;
    border: none !important;
    border-radius: 10px !important;
    color: #fff !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 2px 12px color-mix(in srgb, var(--accent-1) 30%, transparent) !important;
}
#chatmsg-box button[aria-label="Submit"]:hover,
#chatmsg-box button[type=submit]:hover,
[class*="multimodal"] button[aria-label="Submit"]:hover,
div[data-testid="multimodal-textbox"] button[aria-label="Submit"]:hover {
    transform: scale(1.07) !important;
    box-shadow: 0 4px 20px color-mix(in srgb, var(--accent-1) 50%, transparent) !important;
}
/* Upload / attachment buttons */
#chatmsg-box .upload-btn,
[class*="multimodal"] .upload-btn,
div[data-testid="multimodal-textbox"] .upload-btn {
    background: color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    border: 1px solid color-mix(in srgb, var(--accent-1) 20%, transparent) !important;
    border-radius: 8px !important;
    color: color-mix(in srgb, var(--accent-1) 65%, transparent) !important;
    box-shadow: none !important;
}
#chatmsg-box .upload-btn:hover,
[class*="multimodal"] .upload-btn:hover,
div[data-testid="multimodal-textbox"] .upload-btn:hover {
    background: color-mix(in srgb, var(--accent-1) 18%, transparent) !important;
    color: var(--accent-1) !important;
    transform: none !important;
}

/* ── Quick Prompts ────────────────────────────────────────────── */
/* Label text above the examples table */
.examples-holder > .label-wrap span,
.examples-holder > .label-wrap,
.examples-holder .label-wrap span,
.examples-holder label,
.examples-holder label span,
[class*="examples"] .label-wrap span,
[class*="examples"] label span {
    color: var(--text-label) !important;
    font-size: 0.75rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.4px !important;
}
.examples-holder { background: transparent !important; }
.examples-holder table { border-collapse: separate !important; border-spacing: 4px !important; }
.examples-holder table td {
    background: var(--bg-sidebar) !important;
    border: 1px solid color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    border-radius: 18px !important;
    color: var(--text-muted) !important;
    font-size: 0.73rem !important;
    padding: 5px 12px !important;
    transition: all 0.18s ease !important;
    cursor: pointer !important;
}
.examples-holder table td:hover {
    background: color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    border-color: color-mix(in srgb, var(--accent-1) 30%, transparent) !important;
    color: var(--accent-1) !important;
    transform: translateY(-1px) !important;
}

/* ── Gradio Chatbot component — theme-aware ──────────────────── */
/* Chatbot outer container — fill with theme bg */
.gradio-container .chatbot,
.gradio-container [class*="chatbot"],
.gradio-container .chat-wrap,
.gradio-container .wrap.svelte-byatnx {
    background: var(--bg-chatbot) !important;
    color: var(--text-primary) !important;
}
/* Gradio Chatbot inner panel and ALL nested divs */
.gradio-container .gr-chatbot,
.gradio-container div[data-testid="chatbot"],
.gradio-container div[data-testid="chatbot"] > div,
.gradio-container div[data-testid="chatbot"] > div > div,
.gradio-container div[data-testid="chatbot"] .wrap,
.gradio-container div[data-testid="chatbot"] .scroll-hide,
.gradio-container div[data-testid="chatbot"] .overflow-y-auto {
    background: var(--bg-chatbot) !important;
    color: var(--text-primary) !important;
}
/* Border only on the outermost chatbot panel */
.gradio-container div[data-testid="chatbot"] {
    border: 1px solid var(--border-main) !important;
    border-radius: 12px !important;
    overflow: hidden !important;
}
/* Chatbot messages area */
.gradio-container .messages,
.gradio-container .message-wrap,
.gradio-container [class*="messages"] {
    background: transparent !important;
    color: var(--text-primary) !important;
}
/* Individual message text */
.gradio-container .message p,
.gradio-container .message span,
.gradio-container .message div,
.gradio-container .message li,
.gradio-container .message code {
    color: inherit !important;
    background: transparent !important;
}
/* Gradio chatbot component root */
.gradio-container .component-wrap > div,
.gradio-container .overflow-y-auto {
    background: transparent !important;
    color: var(--text-primary) !important;
}
/* Markdown rendered inside messages */
.gradio-container .message .prose,
.gradio-container .message .prose p,
.gradio-container .message .prose li,
.gradio-container .message .prose h1,
.gradio-container .message .prose h2,
.gradio-container .message .prose h3,
.gradio-container .message .prose strong,
.gradio-container .message .prose em {
    color: var(--text-primary) !important;
}
/* Code blocks inside messages */
.gradio-container .message pre,
.gradio-container .message pre code {
    background: color-mix(in srgb, var(--bg-input) 90%, transparent) !important;
    color: var(--text-secondary) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 6px !important;
}
/* Inline code */
.gradio-container .message code:not(pre code) {
    background: color-mix(in srgb, var(--accent-1) 10%, transparent) !important;
    color: var(--accent-1) !important;
    border-radius: 4px !important;
    padding: 1px 5px !important;
}

/* ── Animations ───────────────────────────────────────────────── */
@keyframes glow-pulse {
    0%, 100% { box-shadow: 0 0 5px color-mix(in srgb, var(--dot-color) 40%, transparent); }
    50%       { box-shadow: 0 0 16px color-mix(in srgb, var(--dot-color) 80%, transparent); }
}
@keyframes msg-in {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
}
.message { animation: msg-in 0.22s ease forwards; }

/* ── Floating FABs (top-right) ──────────────────────────────── */
/* Clear-chat FAB — sits to the left of the theme FAB */
#clear-fab-btn {
    position: fixed;
    top: 14px;
    right: 66px;
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: var(--bg-panel);
    border: 1.5px solid var(--border-input);
    cursor: pointer;
    font-size: 1.25rem;
    box-shadow: 0 4px 18px rgba(0,0,0,0.35);
    transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
    user-select: none;
    outline: none;
}
#clear-fab-btn:hover {
    transform: scale(1.1);
    border-color: var(--accent-3);
    box-shadow: 0 6px 24px color-mix(in srgb, var(--accent-3) 30%, transparent);
}
/* Hidden Gradio clear trigger button */
#clear-trigger-btn {
    display: none !important;
}
/* ── Floating Theme Picker (top-right) ───────────────────────── */
#theme-fab {
    position: fixed;
    top: 14px;
    right: 18px;
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: var(--bg-panel);
    border: 1.5px solid var(--border-input);
    cursor: pointer;
    font-size: 1.25rem;
    box-shadow: 0 4px 18px rgba(0,0,0,0.35);
    transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
    user-select: none;
    outline: none;
}
#theme-fab:hover {
    transform: scale(1.1);
    border-color: var(--accent-1);
    box-shadow: 0 6px 24px color-mix(in srgb, var(--accent-1) 30%, transparent);
}
#theme-popover {
    position: fixed;
    top: 62px;
    right: 14px;
    z-index: 9998;
    background: var(--bg-panel);
    border: 1px solid var(--border-input);
    border-radius: 16px;
    padding: 14px 16px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.45);
    backdrop-filter: blur(16px);
    display: none;
    flex-direction: column;
    gap: 10px;
    min-width: 160px;
    animation: popover-in 0.18s ease;
}
#theme-popover.open {
    display: flex;
}
@keyframes popover-in {
    from { opacity: 0; transform: translateY(-8px) scale(0.96); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
}
#theme-popover .pop-title {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--text-label);
    margin-bottom: 2px;
}
#theme-popover .theme-row {
    display: flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    padding: 5px 8px;
    border-radius: 10px;
    transition: background 0.15s ease;
}
#theme-popover .theme-row:hover {
    background: color-mix(in srgb, var(--accent-1) 10%, transparent);
}
.theme-swatch {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    border: 2px solid transparent;
    flex-shrink: 0;
    transition: transform 0.15s ease, border-color 0.15s ease;
}
.theme-swatch.active {
    border-color: #fff;
    box-shadow: 0 0 0 2px rgba(255,255,255,0.3);
    transform: scale(1.15);
}
.theme-row-label {
    font-size: 0.82rem;
    color: var(--text-secondary);
    font-weight: 500;
}

/* ── Hide System Prompt textbox label text ───────────────────── */
/* The cust_prompt Textbox has label="" but Gradio still renders a label-wrap */
#model-select-wrap ~ div .label-wrap,
.gradio-container .sidebar-section-title + div .label-wrap span:empty,
/* Target the label-wrap directly inside the System Prompt textbox block */
[class*="textbox"] > .wrap > .label-wrap,
[class*="textbox"] .label-wrap:has(span:empty),
[class*="textbox"] .label-wrap:has(span[data-testid="block-label"]:empty) {
    display: none !important;
}

/* ── Model Selector (theme-aware, 2/3 width × 1.5× height) ─── */
#model-select-wrap {
    position: relative;
    width: 100% !important;
    max-width: 100% !important;
}
/* Make the inner select control taller (1.5×) */
#model-select-wrap .svelte-select,
#model-select-wrap [class*="svelte-select"],
#model-select-wrap .input-wrap,
#model-select-wrap .value-container {
    min-height: 60px !important;
    height: 60px !important;
    line-height: 60px !important;
}
#model-select-wrap input,
#model-select-wrap input[type=text],
#model-select-wrap input[type=search] {
    height: 60px !important;
    line-height: 60px !important;
}
/* Ensure the dropdown arrow/chevron indicator is always visible */
#model-select-wrap .indicator,
#model-select-wrap [class*="indicator"],
#model-select-wrap .chevron,
#model-select-wrap [class*="chevron"],
#model-select-wrap svg[class*="chevron"],
#model-select-wrap svg[class*="indicator"],
#model-select-wrap .svelte-select .indicator,
#model-select-wrap [class*="svelte-select"] .indicator {
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
    height: auto !important;
    width: auto !important;
    min-height: unset !important;
    line-height: normal !important;
    align-self: center !important;
    flex-shrink: 0 !important;
    color: var(--text-muted) !important;
}
/* Inner svelte-select container */
#model-select-wrap .svelte-select,
#model-select-wrap [class*="svelte-select"],
#model-select-wrap .wrap .wrap,
#model-select-wrap .input-wrap,
#model-select-wrap .value-container {
    background: var(--bg-input) !important;
    color: var(--text-primary) !important;
    border-color: var(--border-input) !important;
}
/* Input text */
#model-select-wrap input,
#model-select-wrap input[type=text],
#model-select-wrap input[type=search] {
    color: var(--text-primary) !important;
    background: transparent !important;
    border: none !important;
}
/* Selected value text */
#model-select-wrap .selection,
#model-select-wrap .selected-item,
#model-select-wrap [class*="selected"] {
    color: var(--text-primary) !important;
    background: transparent !important;
}
/* Dropdown list */
#model-select-wrap .svelte-select-list,
#model-select-wrap [class*="list"],
#model-select-wrap ul[role=listbox],
#model-select-wrap .options {
    background: var(--bg-panel) !important;
    border: 1px solid var(--border-input) !important;
    border-radius: 8px !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4) !important;
    color: var(--text-secondary) !important;
}
#model-select-wrap .svelte-select-list .item,
#model-select-wrap [class*="list"] [class*="item"],
#model-select-wrap li[role=option],
#model-select-wrap .option {
    border-radius: 6px !important;
    font-size: 0.88rem !important;
    color: var(--text-secondary) !important;
    background: transparent !important;
    transition: background 0.15s ease !important;
}
#model-select-wrap .svelte-select-list .item:hover,
#model-select-wrap .svelte-select-list .item.active,
#model-select-wrap li[role=option]:hover,
#model-select-wrap li[role=option][aria-selected=true],
#model-select-wrap .option:hover,
#model-select-wrap .option.active {
    background: color-mix(in srgb, var(--accent-1) 15%, transparent) !important;
    color: var(--text-primary) !important;
}
"""

###########################################################################

# ===================================================================
# 🧩 全局变量（仅用于前端状态管理）
# ===================================================================
_func_mode = 0
_question = ""
_sys_prompt = None
chat_history = []  # 可用于临时缓存（实际应由后端维护）
def on_model_selected(model_name):
    """
    当用户选择模型时触发
    model_name: 用户选择的模型名称
    """
    print(f"Model Selected: {model_name}")
    # 在这里保存到全局变量，或传递给其他函数
    global current_model
    current_model = model_name
    return   # 可以返回值更新其他组件
def update_max_contextsize(model_name):
    try:
        url = "http://127.0.0.1:8910/contextsize"
        params = {"modelName": model_name}  #Llama2.0-7B-SSD
        response = requests.post(url, json=params)
        if response.status_code == 200:
            result = response.json()
            print("context大小:",result["contextsize"])
            return gr.update(maximum=result["contextsize"], value=result["contextsize"])
    except Exception as e:
        print(f"[update_max_contextsize] Error: {e}")
    return gr.update()

def get_mock_profile():
    return "⏱️ 首 Token: 1.2s | 📥 输入速度: 120 tok/s | 📤 输出速度: 45 tok/s"

def stop_generation():
    """中断生成的函数（绑定到 cancel_btn）"""
    service.stopoutput()
    print("✅ The user clicked the pause button and is interrupting the generation...")

def encode_image(image_input):
    if image_input == "":
        return ""
    try:
        if not os.path.exists(image_input):
            raise FileNotFoundError(f"Local file not found: {image_input}")

        with open(image_input, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
            raise Exception(f"Failed to load local image: {e}")

def chat(chatbot, max_length, temp, top_k, top_p, system_prompt=None):
    """对话"""
    if current_model == "":
        gr.Warning("Please select a model first.", duration =5)
        yield chatbot, "", "", ""
        return

    if _question == "":
        gr.Warning("The questions can not be empty.", duration =5)
        yield chatbot, "", "", ""
        return

    answer = ""
    encoded_img = encode_image(_chat_img)
    encoded_audio = encode_image(_chat_audio)
    print('model select: ', current_model)
    for chunk in service.chat({"question": _question, "image": encoded_img, "audio": encoded_audio}, system_prompt=system_prompt, max_length=max_length, temp=temp, top_k=top_k, top_p=top_p, model_name=current_model):
        answer += chunk
        chatbot[-1].content = answer
        yield chatbot, "", "", ""

    profile = service.getchatprofile()
    if profile.status_code == 200:
        # 将响应内容解析为JSON格式
        data = profile.json()

        yield chatbot, round(float(data["time_to_first_token"]), 2), round(float(data["prompt_processing_rate"]),2), round(float(data["token_generation_rate"]), 2)

def summarize_document(chatbot, max_length, temp, top_k, top_p):
    """文档总结（上传文件后触发）"""
    yield chatbot, "", "", ""
    time.sleep(1)
    chatbot.append(ChatMessage(role="assistant", content="Parsing document ..."))
    yield chatbot, "", "", ""
    try:
        docx_summary.load_and_split_docs(file_path=FILE_PATH)
        answer = ""
        for chunk in docx_summary.summarize_map_reduce(custom_prompt="你的工作是用中文写出以下文档的摘要:"):
            answer += chunk
            chatbot[-1].content = answer
            yield chatbot, "", "", ""
    except Exception as e:
        print(f"Exception: {e}")
        yield chatbot, "", "", ""

def translate_text(chatbot, max_length, temp, top_k, top_p):
    """翻译功能"""
    if has_chinese(_question):
        system_prompt = "You are a translation expert, please translate the following sentence into English."
    else:
        system_prompt = "你是一个翻译专家，请将下面的句子翻译成中文"
    answer = ""
    print('chatbot: ', chatbot)
    for chunk in service.chat(_question, system_prompt=system_prompt, max_length=max_length, temp=temp, top_k=top_k, top_p=top_p):
        answer += chunk
        chatbot[-1].content = answer
        yield chatbot, "", "", ""
    profile = service.getchatprofile()

    if profile and profile.status_code == 200:
        # 将响应内容解析为JSON格式
        data = profile.json()

        yield chatbot, data["time_to_first_token"], data["prompt_processing_rate"], data["token_generation_rate"]

def generate_image(chatbot, max_length, temp, top_k, top_p):
    """图像生成"""
    chatbot.append(ChatMessage(role="assistant", content="正在生成图像..."))
    yield chatbot, "", "", ""

    image_path = service.imagegenerate(_question)
    if image_path != "":
        print('image path: ', image_path)

        img = Image.open(image_path)
        img.verify()  # 验证是否是有效图像
        img = Image.open(image_path)  # 重新打开用于保存
        filename = os.path.basename(image_path)

        trusted_path = os.path.join(TRUSTED_OUTPUT_DIR, filename)
        img.save(trusted_path, quality=95, optimize=True)

        chatbot[-1].content = gr.Image(trusted_path)
        yield chatbot, "", "", ""
    else:
        chatbot[-1].content = "服务暂不支持"
        yield chatbot, "", "", ""


# ===================================================================
# 🛠️ 工具函数（保留）
# ===================================================================
def has_chinese(text):
    pattern = re.compile(r'[\u4e00-\u9fff]')
    return pattern.search(text) is not None

def add_media(chatbot, chatmsg):
    """处理用户输入：文本 + 文件上传"""
    global _question, _func_mode, _chat_img, _chat_audio

    _chat_img = ""
    _chat_audio = ""
    _question = chatmsg["text"] or ""
    if _question:
        chatbot.append(ChatMessage(role="user", content=_question))

    # 处理文件上传
    if os.path.exists(FILE_PATH):
        shutil.rmtree(FILE_PATH)
    os.makedirs(FILE_PATH, exist_ok=True)

    for file in chatmsg["files"]:
        file_name = os.path.basename(file)
        shutil.copy(file, os.path.join(FILE_PATH, file_name))
        file_path = os.path.join(FILE_PATH, file_name)
        # if not _chat_img:
        #     _chat_img = file_path

        # 检查文件扩展名，如果是图片则直接显示
        file_ext = os.path.splitext(file_name)[1].lower()
        if file_ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']:
            # 图片文件：直接显示图片
            chatbot.append(ChatMessage(
                role="user",
                content=gr.Image(file_path)
            ))
            _chat_img = file_path
        elif file_ext in AUDIO_TYPES:
            # 音频文件：显示音频播放器
            chatbot.append(ChatMessage(
                role="user",
                content=gr.Audio(file_path)
            ))
            _chat_audio = file_path
        else:
            # 非图片文件：显示文件链接
            chatbot.append(ChatMessage(
                role="user",
                content=f"<small><a href='/gradio_api/file={file_path}' target='_blank'>{file_name}</a></small>"
            ))

    return chatbot, gr.MultimodalTextbox(value=None, interactive=False, submit_btn=False, stop_btn=True)

def vote(data: gr.LikeData):
    if data.liked:
        print("Upvoted:", data.value)
    else:
        print("Downvoted:", data.value)

def reset_state():
    return [], "", "", ""

###################
 #FR0001:Add customized prompt
def update_text(value):
    global _sys_prompt
    _sys_prompt=value
    # print("input:", _sys_prompt)
    with open("customprompt.txt", "w",encoding="utf-8" ) as file:
        file.write(value)

    return None


# ===================================================================
# 🖼️ UI 构建（完全保留原始结构）
# ===================================================================
def main():
    #FR0001:Add customized prompt
    global _sys_prompt
    file_name="customprompt.txt"
    #FR0001:Add customized prompt

    # 模型列表（从服务获取，若服务未启动则为空）
    try:
        model_list = service.getmodellist()
    except Exception:
        model_list = []

    # Pure JS injected at page load via gr.Blocks(js=...) so applyTheme() is globally available
    theme_js_code = """
    const THEMES = {
        dark: {
            '--bg-base':        '#080c12',
            '--bg-panel':       'rgba(10,16,28,0.92)',
            '--bg-sidebar':     'rgba(255,255,255,0.025)',
            '--bg-input':       'rgba(15,22,36,0.95)',
            '--bg-msg-user':    'linear-gradient(135deg, rgba(96,165,250,0.18) 0%, rgba(167,139,250,0.14) 100%)',
            '--bg-msg-bot':     'rgba(255,255,255,0.04)',
            '--border-main':    'rgba(96,165,250,0.1)',
            '--border-sidebar': 'rgba(99,179,237,0.08)',
            '--border-input':   'rgba(96,165,250,0.22)',
            '--border-msg-user':'rgba(96,165,250,0.25)',
            '--border-msg-bot': 'rgba(255,255,255,0.07)',
            '--accent-1':       '#60a5fa',
            '--accent-2':       '#a78bfa',
            '--accent-3':       '#f472b6',
            '--text-primary':   '#e2e8f0',
            '--text-secondary': '#cbd5e1',
            '--text-muted':     'rgba(148,185,225,0.55)',
            '--text-label':     'rgba(99,179,237,0.6)',
            '--header-bg':      'linear-gradient(135deg, #0a1628 0%, #0f2340 50%, #162d4a 100%)',
            '--header-border':  'rgba(99,179,237,0.12)',
            '--btn-grad':       'linear-gradient(135deg, #3b82f6, #8b5cf6)',
            '--scrollbar':      'rgba(99,179,237,0.2)',
            '--chat-panel-bg':  'linear-gradient(180deg, rgba(10,16,28,0.92) 0%, rgba(8,12,18,0.96) 100%)',
            '--section-title':  'rgba(96,165,250,0.45)',
            '--dot-color':      '#34d399',
            '--bg-button':      'rgba(255,255,255,0.04)',
            '--bg-chatbot':     'rgba(8,12,18,0.6)',
            '--shadow-dark':    'rgba(0,0,0,0.35)',
            '--inset-shine':    'rgba(255,255,255,0.04)',
        },
        ocean: {
            '--bg-base':        '#020d1a',
            '--bg-panel':       'rgba(2,18,38,0.94)',
            '--bg-sidebar':     'rgba(0,180,255,0.03)',
            '--bg-input':       'rgba(2,22,48,0.96)',
            '--bg-msg-user':    'linear-gradient(135deg, rgba(0,200,255,0.18) 0%, rgba(0,120,200,0.14) 100%)',
            '--bg-msg-bot':     'rgba(0,180,255,0.04)',
            '--border-main':    'rgba(0,200,255,0.12)',
            '--border-sidebar': 'rgba(0,180,255,0.1)',
            '--border-input':   'rgba(0,200,255,0.28)',
            '--border-msg-user':'rgba(0,200,255,0.3)',
            '--border-msg-bot': 'rgba(0,180,255,0.08)',
            '--accent-1':       '#00c8ff',
            '--accent-2':       '#0080ff',
            '--accent-3':       '#00e5cc',
            '--text-primary':   '#d0f0ff',
            '--text-secondary': '#a0d8ef',
            '--text-muted':     'rgba(100,200,240,0.55)',
            '--text-label':     'rgba(0,200,255,0.65)',
            '--header-bg':      'linear-gradient(135deg, #020d1a 0%, #031a30 50%, #042540 100%)',
            '--header-border':  'rgba(0,200,255,0.15)',
            '--btn-grad':       'linear-gradient(135deg, #0080ff, #00c8ff)',
            '--scrollbar':      'rgba(0,200,255,0.22)',
            '--chat-panel-bg':  'linear-gradient(180deg, rgba(2,18,38,0.94) 0%, rgba(2,12,24,0.97) 100%)',
            '--section-title':  'rgba(0,200,255,0.5)',
            '--dot-color':      '#00e5cc',
            '--bg-button':      'rgba(0,180,255,0.05)',
            '--bg-chatbot':     'rgba(2,10,22,0.6)',
            '--shadow-dark':    'rgba(0,0,0,0.4)',
            '--inset-shine':    'rgba(0,200,255,0.04)',
        },
        forest: {
            '--bg-base':        '#050f08',
            '--bg-panel':       'rgba(5,18,10,0.93)',
            '--bg-sidebar':     'rgba(50,200,80,0.025)',
            '--bg-input':       'rgba(6,22,12,0.96)',
            '--bg-msg-user':    'linear-gradient(135deg, rgba(52,211,153,0.18) 0%, rgba(16,185,129,0.14) 100%)',
            '--bg-msg-bot':     'rgba(52,211,153,0.04)',
            '--border-main':    'rgba(52,211,153,0.1)',
            '--border-sidebar': 'rgba(52,211,153,0.08)',
            '--border-input':   'rgba(52,211,153,0.25)',
            '--border-msg-user':'rgba(52,211,153,0.28)',
            '--border-msg-bot': 'rgba(52,211,153,0.07)',
            '--accent-1':       '#34d399',
            '--accent-2':       '#10b981',
            '--accent-3':       '#6ee7b7',
            '--text-primary':   '#d1fae5',
            '--text-secondary': '#a7f3d0',
            '--text-muted':     'rgba(110,231,183,0.55)',
            '--text-label':     'rgba(52,211,153,0.65)',
            '--header-bg':      'linear-gradient(135deg, #050f08 0%, #071a0e 50%, #0a2414 100%)',
            '--header-border':  'rgba(52,211,153,0.12)',
            '--btn-grad':       'linear-gradient(135deg, #059669, #34d399)',
            '--scrollbar':      'rgba(52,211,153,0.22)',
            '--chat-panel-bg':  'linear-gradient(180deg, rgba(5,18,10,0.93) 0%, rgba(4,12,7,0.97) 100%)',
            '--section-title':  'rgba(52,211,153,0.5)',
            '--dot-color':      '#6ee7b7',
            '--bg-button':      'rgba(52,211,153,0.05)',
            '--bg-chatbot':     'rgba(4,10,6,0.6)',
            '--shadow-dark':    'rgba(0,0,0,0.4)',
            '--inset-shine':    'rgba(52,211,153,0.04)',
        },
        sunset: {
            '--bg-base':        '#0f0510',
            '--bg-panel':       'rgba(20,8,28,0.93)',
            '--bg-sidebar':     'rgba(244,114,182,0.025)',
            '--bg-input':       'rgba(22,8,32,0.96)',
            '--bg-msg-user':    'linear-gradient(135deg, rgba(244,114,182,0.18) 0%, rgba(167,139,250,0.14) 100%)',
            '--bg-msg-bot':     'rgba(244,114,182,0.04)',
            '--border-main':    'rgba(244,114,182,0.1)',
            '--border-sidebar': 'rgba(244,114,182,0.08)',
            '--border-input':   'rgba(244,114,182,0.25)',
            '--border-msg-user':'rgba(244,114,182,0.28)',
            '--border-msg-bot': 'rgba(244,114,182,0.07)',
            '--accent-1':       '#f472b6',
            '--accent-2':       '#a78bfa',
            '--accent-3':       '#fb923c',
            '--text-primary':   '#fce7f3',
            '--text-secondary': '#fbcfe8',
            '--text-muted':     'rgba(251,207,232,0.55)',
            '--text-label':     'rgba(244,114,182,0.65)',
            '--header-bg':      'linear-gradient(135deg, #0f0510 0%, #1a0820 50%, #220a2c 100%)',
            '--header-border':  'rgba(244,114,182,0.12)',
            '--btn-grad':       'linear-gradient(135deg, #db2777, #a78bfa)',
            '--scrollbar':      'rgba(244,114,182,0.22)',
            '--chat-panel-bg':  'linear-gradient(180deg, rgba(20,8,28,0.93) 0%, rgba(14,5,20,0.97) 100%)',
            '--section-title':  'rgba(244,114,182,0.5)',
            '--dot-color':      '#fb923c',
            '--bg-button':      'rgba(244,114,182,0.05)',
            '--bg-chatbot':     'rgba(14,5,20,0.6)',
            '--shadow-dark':    'rgba(0,0,0,0.4)',
            '--inset-shine':    'rgba(244,114,182,0.04)',
        },
        light: {
            '--bg-base':        '#f0f4f8',
            '--bg-panel':       'rgba(255,255,255,0.92)',
            '--bg-sidebar':     'rgba(0,0,0,0.025)',
            '--bg-input':       'rgba(255,255,255,0.95)',
            '--bg-msg-user':    'linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(139,92,246,0.1) 100%)',
            '--bg-msg-bot':     'rgba(0,0,0,0.03)',
            '--border-main':    'rgba(59,130,246,0.15)',
            '--border-sidebar': 'rgba(59,130,246,0.1)',
            '--border-input':   'rgba(59,130,246,0.3)',
            '--border-msg-user':'rgba(59,130,246,0.3)',
            '--border-msg-bot': 'rgba(0,0,0,0.08)',
            '--accent-1':       '#2563eb',
            '--accent-2':       '#7c3aed',
            '--accent-3':       '#db2777',
            '--text-primary':   '#1e293b',
            '--text-secondary': '#334155',
            '--text-muted':     'rgba(71,85,105,0.7)',
            '--text-label':     'rgba(37,99,235,0.75)',
            '--header-bg':      'linear-gradient(135deg, #dbeafe 0%, #ede9fe 50%, #fce7f3 100%)',
            '--header-border':  'rgba(59,130,246,0.2)',
            '--btn-grad':       'linear-gradient(135deg, #2563eb, #7c3aed)',
            '--scrollbar':      'rgba(59,130,246,0.25)',
            '--chat-panel-bg':  'linear-gradient(180deg, rgba(255,255,255,0.92) 0%, rgba(240,244,248,0.96) 100%)',
            '--section-title':  'rgba(37,99,235,0.55)',
            '--dot-color':      '#10b981',
            '--bg-button':      'rgba(37,99,235,0.06)',
            '--bg-chatbot':     'rgba(255,255,255,0.85)',
            '--shadow-dark':    'rgba(0,0,0,0.08)',
            '--inset-shine':    'rgba(255,255,255,0.6)',
        }
    };

    window.applyTheme = function(name) {
        const theme = THEMES[name];
        if (!theme) return;
        const root = document.documentElement;
        Object.entries(theme).forEach(([k, v]) => root.style.setProperty(k, v));
        try { localStorage.setItem('genie_theme', name); } catch(e) {}
        document.querySelectorAll('.theme-swatch').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.theme === name);
        });
    };

    window.toggleThemePopover = function(e) {
        e.stopPropagation();
        const pop = document.getElementById('theme-popover');
        if (pop) pop.classList.toggle('open');
    };

    window.closeThemePopover = function() {
        const pop = document.getElementById('theme-popover');
        if (pop) pop.classList.remove('open');
    };

    // Close popover when clicking outside
    document.addEventListener('click', function(e) {
        const pop = document.getElementById('theme-popover');
        const fab = document.getElementById('theme-fab');
        if (pop && pop.classList.contains('open') &&
            !pop.contains(e.target) && e.target !== fab) {
            pop.classList.remove('open');
        }
    });

    // Restore saved theme on load
    (function() {
        const saved = (function(){ try { return localStorage.getItem('genie_theme'); } catch(e){ return null; } })();
        if (saved && THEMES[saved]) window.applyTheme(saved);
    })();
    """

    with gr.Blocks(fill_width=True, fill_height=True, js=theme_js_code, css=css) as demo:
        demo.title = "Genie App"

        gr.set_static_paths(paths=["resources/", "files/"])


        gr.HTML("""
        <div id="genie-header">
            <span style="font-size:2rem;">🧞</span>
            <div>
                <div style="font-size:1.4rem;font-weight:800;background:linear-gradient(90deg,#60a5fa,#a78bfa,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;">Genie App</div>
                <div style="font-size:0.7rem;color:rgba(148,185,225,0.6);letter-spacing:1px;text-transform:uppercase;">On-Device AI Assistant</div>
            </div>
        </div>
        """)

        gr.HTML("""
        <!-- Clear chat FAB -->
        <button id="clear-fab-btn" onclick="document.getElementById('clear-trigger-btn').click()" title="Clear Chat">🗑️</button>
        <!-- Floating theme picker FAB -->
        <button id="theme-fab" onclick="toggleThemePopover(event)" title="Change Theme">👕</button>

        <!-- Theme popover panel -->
        <div id="theme-popover">
            <div class="pop-title">🎨 &nbsp;Theme</div>
            <div class="theme-row" onclick="applyTheme('dark');closeThemePopover()">
                <span class="theme-swatch" data-theme="dark"
                    style="background:linear-gradient(135deg,#080c12,#162d4a);"></span>
                <span class="theme-row-label">Dark</span>
            </div>
            <div class="theme-row" onclick="applyTheme('ocean');closeThemePopover()">
                <span class="theme-swatch" data-theme="ocean"
                    style="background:linear-gradient(135deg,#020d1a,#00c8ff);"></span>
                <span class="theme-row-label">Ocean</span>
            </div>
            <div class="theme-row" onclick="applyTheme('forest');closeThemePopover()">
                <span class="theme-swatch" data-theme="forest"
                    style="background:linear-gradient(135deg,#050f08,#34d399);"></span>
                <span class="theme-row-label">Forest</span>
            </div>
            <div class="theme-row" onclick="applyTheme('sunset');closeThemePopover()">
                <span class="theme-swatch" data-theme="sunset"
                    style="background:linear-gradient(135deg,#0f0510,#f472b6);"></span>
                <span class="theme-row-label">Sunset</span>
            </div>
            <div class="theme-row" onclick="applyTheme('light');closeThemePopover()">
                <span class="theme-swatch" data-theme="light"
                    style="background:linear-gradient(135deg,#dbeafe,#ede9fe);border:1.5px solid rgba(0,0,0,0.12);"></span>
                <span class="theme-row-label">Light</span>
            </div>
        </div>
        """)

        with gr.Tab("💬 Chat"):
            with gr.Row(equal_height=True):

                # ── Left sidebar ───────────────────────────────────────
                with gr.Column(scale=2):

                    gr.HTML('<div class="sidebar-section-title">⚙️ &nbsp;Model</div>')
                    model_select = gr.Dropdown(
                        choices=model_list, label="Active Model",
                        interactive=True, allow_custom_value=False,
                        elem_id="model-select-wrap"
                    )
                    with gr.Row():
                        connect_btn = gr.Button("🔗 Connect", scale=1, variant="primary")
                    connect_status = gr.Textbox(label="Status", value="", interactive=False, lines=1)

                    gr.HTML('<div class="sidebar-section-title">🎛️ &nbsp;Generation</div>')
                    max_length = gr.Slider(1, 4096, value=4096, step=1,   label="Max Length",  interactive=True)
                    temp       = gr.Slider(0.01, 1, value=0.8,  step=0.01, label="Temperature", interactive=True)
                    top_k      = gr.Slider(1, 100, value=40,    step=1,    label="Top K",       interactive=True)
                    top_p      = gr.Slider(0, 1,   value=0.95,  step=0.01, label="Top P",       interactive=True)

                    gr.HTML('<div class="sidebar-section-title">📊 &nbsp;Performance</div>')
                    f_latency = gr.Textbox(label="⏱ First Token (s)", value="—", interactive=False, elem_classes="metric-box")
                    with gr.Row():
                        p_speed = gr.Textbox(label="📥 Prompt tok/s", value="—", interactive=False, elem_classes="metric-box")
                        e_speed = gr.Textbox(label="📤 Eval tok/s",   value="—", interactive=False, elem_classes="metric-box")

                    gr.HTML('<div class="sidebar-section-title">🍸 &nbsp;System Prompt</div>')
                    #FR0001:Add customized prompt
                    cust_prompt = gr.Textbox(
                        label="", value="", visible=False,
                        interactive=True, lines=4,
                        placeholder="Enter a custom system prompt…"
                    )

                # ── Main chat area ─────────────────────────────────────
                with gr.Column(scale=8, elem_id="chat-col"):
                    chatbot = gr.Chatbot(scale=9, height="52vh", elem_classes="chatbot-wrap", show_label=False,
                                        avatar_images=("resources/user_avatar.svg", "resources/bot_avatar.svg"))
                    chatbot.like(vote, None, None)

                    # Function mode buttons
                    with gr.Row():
                        func_1_btn = gr.Button(FUNC_LIST_EN[0], elem_classes="button_cls")
                        func_2_btn = gr.Button(FUNC_LIST_EN[1], elem_classes="button_cls")
                        func_3_btn = gr.Button(FUNC_LIST_EN[2], elem_classes="button_cls")
                        func_6_btn = gr.Button(FUNC_LIST_EN[5], elem_classes="button_cls")
                        #FR0001:Add customized prompt
                        func_7_btn = gr.Button(FUNC_LIST_EN[6], elem_classes="button_cls")

                    chatmsg = gr.MultimodalTextbox(
                        scale=1, interactive=True, file_count="multiple",
                        placeholder="✦  Ask anything, or drop a file…",
                        show_label=True, autofocus=True,
                        max_plain_text_length=3000,
                        file_types=IMG_TYPES + AUDIO_TYPES,
                        label=FUNC_LIST_EN[_func_mode],
                        elem_id="chatmsg-box"
                    )
                    gr.Examples(
                        ["Summarize the document content",
                         "Analyze the source code and give line-by-line comments.",
                         "Inquire about the weather in Shanghai today",
                         "Help me check the following English grammar…"],
                        chatmsg, label="✨ Quick Prompts"
                    )
                    clear_btn = gr.Button("🗑️", elem_id="clear-trigger-btn")

        # ===================================================================
        # 🔗 事件绑定（保留结构，逻辑由 predict 分发）
        # ===================================================================
        def predict(chatbot, max_length, temp, top_k, top_p):
            global _question, _func_mode, _sys_prompt

            if not _question.strip() and _func_mode != 1:  # 文档总结除外
                yield chatbot, "", "", ""
                return

            chatbot.append(ChatMessage(role="assistant", content=""))

            if FUNC_LIST[_func_mode] == "📐 解题答疑":
                for chunk in chat(chatbot, max_length, temp, top_k, top_p):
                    yield chunk
                chatbot.append(ChatMessage(role="assistant", content=""))

            elif FUNC_LIST[_func_mode] == "📚 文档总结":
                for chunk in summarize_document(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🗛 AI 翻 译":
                for chunk in translate_text(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🎨 图像生成":
                for chunk in generate_image(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🍸 定制功能":
                for chunk in chat(chatbot, max_length, temp, top_k, top_p, _sys_prompt):
                    yield chunk
                chatbot.append(ChatMessage(role="assistant", content=""))

            else:
                chatbot[-1].content = "该功能正在开发中..."
                yield chatbot, "", "", ""

        # 事件链：输入 → add_media → predict → 恢复输入框
        chat_run = chatmsg.submit(add_media, [chatbot, chatmsg], [chatbot, chatmsg])
        chat_run = chat_run.then(predict, [chatbot, max_length, temp, top_k, top_p], [chatbot, f_latency, p_speed, e_speed])
        chat_run.then(lambda: gr.MultimodalTextbox(interactive=True, submit_btn=True, stop_btn=False), None, chatmsg)
        chatmsg.stop(fn=stop_generation)

        #FR0001:Add customized prompt
        if not os.path.exists(file_name):
            with open(file_name, "w",encoding="utf-8") as file:
                file.write("")  # Create an empty file

        with open(file_name, "r",encoding="utf-8") as file:
            cust_prompt.value = file.read()

        cust_prompt.change(update_text, inputs=cust_prompt, outputs=None)
        _sys_prompt = cust_prompt.value
        #FR0001:Add customized prompt

        # 功能按钮切换
        def func_change(func_mode):
            global _func_mode
            global _sys_prompt

            _sys_prompt = cust_prompt.value
            # print("\nchange:sys prompt:", _sys_prompt)

            _func_mode = func_mode
            func_name = FUNC_LIST[func_mode]
            if func_name == "📚 文档总结":
                return gr.MultimodalTextbox(label=func_name, sources=["upload"], file_types=FILE_TYPES)
            elif func_name == "📐 解题答疑":
                return gr.MultimodalTextbox(label=func_name, sources=["upload"], file_types=IMG_TYPES + AUDIO_TYPES)
            else:
                return gr.MultimodalTextbox(label=func_name, sources=[])

        clear_btn.click(reset_state, None, [chatbot, f_latency, p_speed, e_speed])

        func_1_btn.click(lambda: func_change(0), None, chatmsg)
        func_2_btn.click(lambda: func_change(1), None, chatmsg)
        func_3_btn.click(lambda: func_change(2), None, chatmsg)
        func_6_btn.click(lambda: func_change(5), None, chatmsg)
        #FR0001:Add customized prompt
        func_7_btn.click(lambda: func_change(6), None, [chatmsg])

        # 刷新模型列表
        def refresh_model_list():
            try:
                models = service.getmodellist()
                if models:
                    return gr.update(choices=models, value=models[0]), f"✅ Found {len(models)} model(s)."
                else:
                    return gr.update(choices=[], value=None), "⚠️ No models returned by service."
            except Exception as e:
                import traceback
                traceback.print_exc()
                return gr.update(choices=[], value=None), f"❌ Failed to fetch models: {e}"

        # 连接/加载选中的模型
        def connect_model(model_name):
            if not model_name:
                return "⚠️ Please select a model first."
            try:
                url = "http://127.0.0.1:8910/models"
                resp = requests.post(url, json={"model": model_name}, timeout=30)
                if resp.status_code == 200:
                    global current_model
                    current_model = model_name
                    return f"✅ Model '{model_name}' connected successfully."
                else:
                    return f"⚠️ Server returned {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                return f"❌ Failed to connect: {e}"

        connect_btn.click(refresh_model_list, None, [model_select, connect_status])

        # 页面加载时初始化 current_model
        def init_current_model(model_name):
            global current_model
            if model_name:
                current_model = model_name
                print(f"[init] current_model set to: {model_name}")

        demo.load(init_current_model, inputs=model_select, outputs=None)

        # 模型切换：直接加载选中的模型
        model_select.change(
            connect_model,       # 选中即加载
            model_select,
            connect_status
        ).then(
            on_model_selected,   # 更新全局 current_model
            model_select,
            None
        ).then(
            update_max_contextsize,   # 更新 max_length 的范围和值
            model_select,
            max_length
        )

    demo.queue().launch(server_name=HOST, share=False, inbrowser=False, server_port=PORT)

# ===================================================================
# 🖥️  Standalone Chat UI — served on UI_PORT
# ===================================================================
UI_PORT = 7861

def _serve_chat_ui():
    """Serve the webui/ directory so chat.html is accessible at http://localhost:UI_PORT/chat.html"""
    webui_dir = os.path.dirname(os.path.abspath(__file__))

    # Paths that should be proxied to the Genie API Service on port 8910
    _API_PREFIXES = ('/models', '/v1/', '/stop', '/clear', '/reload',
                     '/servicestop', '/contextsize', '/profile', '/images/')

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=webui_dir, **kwargs)

        def log_message(self, fmt, *args):
            # Print all access logs so the user can see every request
            print(f"[ChatUI] {self.address_string()} - " + (fmt % args))

        def _is_api_path(self):
            return self.path.split('?')[0].rstrip('/') in ('', ) or \
                   self.path.startswith(_API_PREFIXES)

        def _proxy_to_backend(self):
            """Forward the request to GenieAPIService on port 8910 and relay the response."""
            target_url = f"http://127.0.0.1:8910{self.path}"
            print(f"[ChatUI] >>> Proxy {self.command} {self.path} -> {target_url}")
            print(f"[ChatUI]     Request headers: {dict(self.headers)}")
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
                body = self.rfile.read(length) if length > 0 else None
                if body:
                    print(f"[ChatUI]     Request body ({length} bytes): {body[:500]}")
                method = self.command
                fwd_headers = {}
                for h in ('Content-Type', 'Authorization', 'Accept'):
                    v = self.headers.get(h)
                    if v:
                        fwd_headers[h] = v
                print(f"[ChatUI]     Forwarding headers: {fwd_headers}")
                # Always use stream=True so SSE / chunked responses are relayed correctly
                resp = requests.request(method, target_url, headers=fwd_headers,
                                        data=body, stream=True, timeout=60)
                print(f"[ChatUI] <<< Proxy response: HTTP {resp.status_code}")
                print(f"[ChatUI]     Response headers: {dict(resp.headers)}")
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() in ('content-encoding', 'transfer-encoding', 'connection', 'content-length'):
                        continue
                    self.send_header(k, v)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                # Stream the response body chunk by chunk
                for chunk in resp.iter_content(chunk_size=4096):
                    if chunk:
                        self.wfile.write(chunk)
            except Exception as e:
                import traceback
                print(f"[ChatUI] Proxy ERROR: {e}")
                traceback.print_exc()
                try:
                    self.send_error(502, f"Proxy error: {e}")
                except Exception:
                    pass

        def do_GET(self):
            print(f"[ChatUI] GET {self.path}")
            if self.path.startswith(_API_PREFIXES):
                self._proxy_to_backend()
            else:
                # Send no-cache headers for HTML so browser always gets fresh content
                if self.path.endswith('.html') or self.path == '/':
                    self.send_response(200)
                    path = self.translate_path(self.path)
                    print(f"[ChatUI] Serving HTML file: {path}")
                    try:
                        with open(path, 'rb') as f:
                            data = f.read()
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(data)))
                        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                        self.send_header('Pragma', 'no-cache')
                        self.send_header('Expires', '0')
                        self.end_headers()
                        self.wfile.write(data)
                        print(f"[ChatUI] Served {len(data)} bytes for {self.path}")
                    except Exception as e:
                        print(f"[ChatUI] ERROR serving HTML: {e}")
                        super().do_GET()
                else:
                    super().do_GET()

        def do_POST(self):
            if self.path.startswith(_API_PREFIXES):
                self._proxy_to_backend()
            else:
                self.send_error(405, "Method Not Allowed")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
            self.end_headers()

    socketserver.TCPServer.allow_reuse_address = True
    print(f"[ChatUI] Starting HTTP server on port {UI_PORT}, serving directory: {webui_dir}")
    print(f"[ChatUI] API prefixes that will be proxied to port 8910: {_API_PREFIXES}")
    try:
        with socketserver.TCPServer(("", UI_PORT), _Handler) as httpd:
            print(f"[ChatUI] HTTP server started on port {UI_PORT}")
            print(f"[ChatUI] Open browser at: http://localhost:{UI_PORT}/chat.html")
            httpd.serve_forever()
    except Exception as e:
        import traceback
        print(f"[ChatUI] ERROR: Failed to start HTTP server on port {UI_PORT}: {e}")
        traceback.print_exc()

def launch_chat_ui():
    """Start the HTML server in a daemon thread, then open the browser."""
    t = threading.Thread(target=_serve_chat_ui, daemon=True)
    t.start()
    time.sleep(1.5)  # give server more time to bind
    url = f"http://localhost:{UI_PORT}/chat.html"
    print(f"\n{'='*60}")
    print(f"  🧞  Genie Chat UI  →  {url}")
    print(f"{'='*60}\n")
    webbrowser.open(url)

if __name__ == '__main__':
    launch_chat_ui()
    main()
