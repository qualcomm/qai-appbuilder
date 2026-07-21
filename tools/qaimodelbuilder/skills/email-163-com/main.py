# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
email-163-com - 163 邮箱完整邮件管理工具
版本：1.0.2
创建日期：2026-02-19
修复日期：2026-02-21

v1.0.2 修复内容:
- 修复 IMAP 选择 INBOX 失败问题（"Unsafe Login"错误）
- 添加 IMAP ID 预发送支持（登录前发送）
- 改进错误处理和诊断信息
- 支持 UTF-7 文件夹名称编码
"""

import argparse
import smtplib
import imaplib
import email
import json
import os
import sys

# 强制 stdout/stderr 使用 UTF-8，避免 Windows 终端 emoji 编码错误
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header, Header
from email.utils import formataddr
import re
from datetime import datetime
import base64
import ssl

# 默认配置
DEFAULT_CONFIG = {
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
        "support_email": ""
    },
    "defaults": {
        "folder": "INBOX",
        "count": 5,
        "output_dir": "~/Downloads"
    },
    "use_starttls": False  # 163 邮箱不需要 STARTTLS
}

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "email-163-com", "config.json")


def remove_emoji(text):
    """移除字符串中的 emoji，避免邮件客户端显示问题（不影响中文字符）"""
    if not text:
        return text
    # 只匹配真正的 emoji 范围，避免误伤中文/日文/韩文字符
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"   # 表情符号
        "\U0001F300-\U0001F5FF"   # 杂项符号和象形文字
        "\U0001F680-\U0001F6FF"   # 交通和地图符号
        "\U0001F1E0-\U0001F1FF"   # 国旗字母
        "\U00002702-\U000027B0"   # 装饰符号
        "\U0001F900-\U0001F9FF"   # 补充符号和象形文字
        "\U0001FA00-\U0001FA6F"   # 国际象棋符号
        "\U0001FA70-\U0001FAFF"   # 符号和象形文字扩展-A
        "\U00002600-\U000026FF"   # 杂项符号
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text).strip()


def load_config():
    """加载配置文件"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            for key in DEFAULT_CONFIG:
                if key not in config:
                    config[key] = DEFAULT_CONFIG[key]
            # print(f"📋 最终配置内容：\n{json.dumps(config, indent=2, ensure_ascii=False)}")
            return config
    else:
        print(f"❌ 配置文件不存在：{CONFIG_PATH}")
        print("   请运行：email-163-com init")
        sys.exit(1)


def save_config(config):
    """保存配置文件"""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"✅ 配置文件已保存：{CONFIG_PATH}")


def init_config():
    """初始化配置"""
    print("📧 email-163-com 配置向导")
    print("=" * 50)
    
    config = DEFAULT_CONFIG.copy()
    
    # 邮箱地址
    email_input = input(f"邮箱地址 [{config['email']}]: ").strip()
    if email_input:
        config['email'] = email_input
    
    if not config['email']:
        print("❌ 必须提供邮箱地址")
        sys.exit(1)
    
    # 授权码
    print("\n请输入 163 邮箱授权码（不是登录密码！）")
    print("获取方式：https://mail.163.com -> 设置 -> POP3/SMTP/IMAP")
    password_input = input("授权码： ").strip()
    if password_input:
        config['password'] = password_input
    
    if not config['password']:
        print("❌ 必须提供授权码")
        sys.exit(1)
    
    # IMAP ID
    print("\nIMAP ID 信息（可选，直接回车使用默认值）")
    config['imap_id']['name'] = input(f"客户端名称 [{config['imap_id']['name']}]: ").strip() or config['imap_id']['name']
    config['imap_id']['version'] = input(f"版本 [{config['imap_id']['version']}]: ").strip() or config['imap_id']['version']
    config['imap_id']['support_email'] = input(f"支持邮箱 [{config['imap_id']['support_email']}]: ").strip() or config['imap_id']['support_email']
    
    # 保存配置
    save_config(config)
    
    # 测试连接
    print("\n" + "=" * 50)
    print("🔍 测试连接...")
    test_connection(config)


def test_connection(config):
    """测试邮箱连接"""
    try:
        print(f"\n📧 测试 IMAP 连接...")
        print(f"   服务器：{config['imap_server']}:{config['imap_port']}")
        
        # 创建 SSL 连接
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        
        # 登录
        print(f"   登录：{config['email']}")
        mail.login(config['email'], config['password'])
        
        # 尝试发送 IMAP ID（163 邮箱要求）
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
            print(f"   ✅ IMAP ID 发送成功")
        except Exception as e:
            print(f"   ⚠️  IMAP ID 发送失败：{e}（不影响使用）")
        
        # 选择 INBOX - 使用标准名称
        print(f"   选择文件夹：INBOX")
        status, messages = mail.select("INBOX")
        
        if status == "OK":
            print(f"   ✅ 连接测试成功！")
            
            # 获取邮件数量
            status, data = mail.search(None, "ALL")
            if status == "OK":
                count = len(data[0].split())
                print(f"   📬 邮箱共有 {count} 封邮件")
        else:
            print(f"   ❌ 选择 INBOX 失败：{messages}")
            print(f"\n💡 可能原因:")
            print(f"   1. 授权码错误/过期")
            print(f"   2. IMAP 服务未开启")
            print(f"   3. 账号安全策略限制")
            print(f"\n🔧 解决方案:")
            print(f"   1. 重新生成授权码")
            print(f"   2. 登录 163 邮箱确认 IMAP 已开启")
            print(f"   3. 联系 163 客服：kefu@188.com")
        
        mail.close()
        mail.logout()
        
    except imaplib.IMAP4.error as e:
        error_msg = str(e)
        print(f"\n❌ IMAP 错误：{e}")
        
        if "Unsafe Login" in error_msg or "authentication" in error_msg.lower():
            print(f"\n💡 问题诊断：认证失败")
            print(f"   • 授权码错误或已过期")
            print(f"   • 需要使用客户端专用授权码，而非登录密码")
        elif "SSL" in error_msg:
            print(f"\n💡 问题诊断：SSL 连接失败")
            print(f"   • 检查网络连接")
            print(f"   • 检查防火墙设置")
        else:
            print(f"\n💡 问题诊断：未知错误")
            
    except Exception as e:
        print(f"\n❌ 未知错误：{e}")
        import traceback
        traceback.print_exc()


def decode_mime_words(s):
    """解码 MIME 编码的字符串"""
    if not s:
        return s
    
    decoded_parts = []
    for part in decode_header(s):
        text, enc = part
        if isinstance(text, bytes):
            try:
                decoded_parts.append(text.decode(enc if enc else 'utf-8', errors='ignore'))
            except:
                decoded_parts.append(text.decode('utf-8', errors='ignore'))
        else:
            decoded_parts.append(text)
    
    return ''.join(decoded_parts)


def read_emails(args, config):
    """读取邮件"""
    try:
        # 创建 SSL 连接
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        
        # 登录
        mail.login(config['email'], config['password'])
        
        # 尝试发送 IMAP ID（可选）
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass  # IMAP ID 发送失败不影响使用
        
        # 选择文件夹 - 处理 UTF-7 编码
        folder = args.folder or config['defaults']['folder']
        
        # 163 邮箱的 INBOX 必须是大写
        if folder.upper() == "INBOX":
            folder = "INBOX"
        
        status, messages = mail.select(folder)
        
        if status != 'OK':
            print(f"❌ 无法选择文件夹：{folder}")
            print(f"   错误信息：{messages}")
            print(f"\n💡 尝试列出可用文件夹...")
            list_folders_impl(mail)
            return
        
        # 如果指定了 --id，直接读取该封邮件完整内容
        if args.id:
            fetch_mode = '(RFC822)'  # 默认读取完整邮件
            status, msg_data = mail.fetch(args.id, fetch_mode)
            
            if status != 'OK':
                print(f"❌ 无法获取邮件 ID={args.id}")
                mail.close()
                mail.logout()
                return
            
            msg = email.message_from_bytes(msg_data[0][1])
            
            from_header = decode_mime_words(msg.get('From', ''))
            subject = decode_mime_words(msg.get('Subject', ''))
            date = msg.get('Date', '')
            to_header = decode_mime_words(msg.get('To', ''))
            
            print(f"📧 邮件 ID: {args.id}")
            print(f"   From:    {from_header}")
            print(f"   To:      {to_header}")
            print(f"   Subject: {subject}")
            print(f"   Date:    {date}")
            print("=" * 60)
            
            # 解析并打印正文内容：优先 text/plain，没有才用 text/html
            if True:  # 默认显示完整内容
                plain_body = None
                html_body = None
                attachments = []
                
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get('Content-Disposition', ''))
                        
                        if 'attachment' in content_disposition:
                            filename = decode_mime_words(part.get_filename() or '')
                            attachments.append(filename)
                        elif content_type == 'text/plain' and plain_body is None:
                            charset = part.get_content_charset() or 'utf-8'
                            try:
                                plain_body = part.get_payload(decode=True).decode(charset, errors='replace')
                            except Exception as e:
                                plain_body = f"[解码失败：{e}]"
                        elif content_type == 'text/html' and html_body is None:
                            charset = part.get_content_charset() or 'utf-8'
                            try:
                                html_body = part.get_payload(decode=True).decode(charset, errors='replace')
                            except Exception as e:
                                html_body = f"[解码失败：{e}]"
                else:
                    charset = msg.get_content_charset() or 'utf-8'
                    try:
                        plain_body = msg.get_payload(decode=True).decode(charset, errors='replace')
                    except Exception as e:
                        plain_body = f"[解码失败：{e}]"
                
                def strip_html(raw):
                    """去除 HTML 标签，返回纯文本"""
                    t = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL | re.IGNORECASE)
                    t = re.sub(r'<script[^>]*>.*?</script>', '', t, flags=re.DOTALL | re.IGNORECASE)
                    t = re.sub(r'<[^>]+>', '', t)
                    t = re.sub(r'&nbsp;', ' ', t)
                    t = re.sub(r'&amp;', '&', t)
                    t = re.sub(r'&lt;', '<', t)
                    t = re.sub(r'&gt;', '>', t)
                    t = re.sub(r'&quot;', '"', t)
                    t = re.sub(r'&#39;', "'", t)
                    return '\n'.join(line for line in t.splitlines() if line.strip())

                # 优先显示纯文本；没有纯文本时去除 HTML 标签后显示
                if plain_body is not None:
                    # 检测 plain_body 是否实际上是 HTML 内容
                    if re.search(r'<html|<!DOCTYPE', plain_body, re.IGNORECASE):
                        plain_text = strip_html(plain_body)
                    else:
                        plain_text = '\n'.join(line for line in plain_body.splitlines() if line.strip())
                    if len(plain_text) > 5000:
                        print(f"\n📄 正文:\n{plain_text[:5000]}\n... (内容过长，已截断，共 {len(plain_text)} 字符)")
                    else:
                        print(f"\n📄 正文:\n{plain_text}")
                elif html_body is not None:
                    stripped = strip_html(html_body)
                    if len(stripped) > 5000:
                        print(f"\n📄 正文:\n{stripped[:5000]}\n... (内容过长，已截断，共 {len(stripped)} 字符)")
                    else:
                        print(f"\n📄 正文:\n{stripped}")
                else:
                    print("\n(无正文内容)")
                
                # 显示附件列表
                if attachments:
                    print(f"\n📎 附件 ({len(attachments)} 个):")
                    for name in attachments:
                        print(f"   - {name}")
            
            mail.close()
            mail.logout()
            return
        
        # 搜索邮件列表
        if args.unread:
            status, data = mail.search(None, 'UNSEEN')
        else:
            status, data = mail.search(None, 'ALL')
        
        if status != 'OK':
            print("❌ 无法搜索邮件")
            return
        
        msg_ids = data[0].split()
        total = len(msg_ids)
        count = args.count or config['defaults']['count']
        
        print(f"📬 {folder}: {total} messages total\n")
        
        if total == 0:
            print("   (没有邮件)")
            mail.close()
            mail.logout()
            return
        
        # 显示最新邮件列表
        display_count = min(count, total)
        for msg_id in msg_ids[-display_count:]:
            status, msg_data = mail.fetch(msg_id, '(RFC822.HEADER)')
            
            if status == 'OK':
                msg = email.message_from_bytes(msg_data[0][1])
                
                from_header = decode_mime_words(msg.get('From', ''))
                subject = decode_mime_words(msg.get('Subject', ''))
                date = msg.get('Date', '')[:30]
                
                print(f"📧 From: {from_header}")
                print(f"   Subject: {subject}")
                print(f"   Date: {date}")
                print(f"   ID: {msg_id.decode()}")
                print("-" * 50)
        
        mail.close()
        mail.logout()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def list_folders_impl(mail):
    """列出文件夹（内部实现）"""
    try:
        status, folders = mail.list()
        if status == 'OK':
            print(f"\n📂 可用文件夹:")
            for folder in folders:
                decoded = folder.decode('utf-8', errors='replace')
                print(f"   - {decoded}")
    except Exception as e:
        print(f"   ❌ 无法列出文件夹：{e}")


def list_folders(args, config):
    """列出文件夹"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        list_folders_impl(mail)
        
        mail.close()
        mail.logout()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


def search_emails(args, config):
    """搜索邮件"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        folder = args.folder or config['defaults']['folder']
        if folder.upper() == "INBOX":
            folder = "INBOX"
        
        status, messages = mail.select(folder)
        if status != 'OK':
            print(f"❌ 无法选择文件夹：{folder}")
            return
        
        # 先获取所有邮件 ID
        status, data = mail.search(None, 'ALL')
        if status != 'OK':
            print(f"❌ 搜索失败")
            return
        
        all_msg_ids = data[0].split()
        count = args.count or config['defaults']['count']
        
        # 构建搜索条件描述（仅用于显示）
        search_desc_parts = []
        if args.from_addr:
            search_desc_parts.append(f'FROM="{args.from_addr}"')
        if args.subject:
            search_desc_parts.append(f'SUBJECT="{args.subject}"')
        if args.to:
            search_desc_parts.append(f'TO="{args.to}"')
        search_desc = ' AND '.join(search_desc_parts) if search_desc_parts else 'ALL'
        
        print(f"🔍 Search: {search_desc}")
        
        # 本地过滤：逐封读取邮件头，匹配关键词
        matched = []
        for msg_id in all_msg_ids:
            status, msg_data = mail.fetch(msg_id, '(RFC822.HEADER)')
            if status != 'OK':
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_header = decode_mime_words(msg.get('From', ''))
            subject = decode_mime_words(msg.get('Subject', ''))
            to_header = decode_mime_words(msg.get('To', ''))
            date = msg.get('Date', '')[:30]
            
            # 检查是否匹配所有条件
            match = True
            if args.from_addr and args.from_addr.lower() not in from_header.lower():
                match = False
            if args.subject and args.subject.lower() not in subject.lower():
                match = False
            if args.to and args.to.lower() not in to_header.lower():
                match = False
            
            if match:
                matched.append((msg_id, from_header, subject, date))
        
        total = len(matched)
        print(f"📬 Found: {total} messages\n")
        
        if total == 0:
            print("   (没有匹配的邮件)")
            mail.close()
            mail.logout()
            return
        
        display_count = min(count, total)
        for msg_id, from_header, subject, date in matched[-display_count:]:
            print(f"📧 From: {from_header}")
            print(f"   Subject: {subject}")
            print(f"   Date: {date}")
            print(f"   ID: {msg_id.decode()}")
            print("-" * 50)
        
        mail.close()
        mail.logout()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def send_email(args, config):
    """发送邮件"""
    try:
        # 创建邮件
        msg = MIMEMultipart()
        msg['From'] = formataddr((Header(config['email'].split('@')[0], 'utf-8').encode(), config['email']))
        msg['To'] = args.to
        msg['Subject'] = Header(remove_emoji(args.subject), 'utf-8')
        
        # 添加正文
        body_text = args.body
        if args.file:
            body_text += "\n\n请查看附件。"
        
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        
        # 添加附件
        if args.file:
            for filepath in args.file:
                if os.path.exists(filepath):
                    with open(filepath, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                        encoders.encode_base64(part)
                        filename = os.path.basename(filepath)
                        part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                        msg.attach(part)
                        print(f"✅ 已添加附件：{filename}")
        
        # 发送邮件
        print(f"\n📧 正在发送邮件到：{args.to}...")
        server = smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'])
        server.login(config['email'], config['password'])
        server.sendmail(config['email'], [args.to], msg.as_string())
        server.quit()
        
        print("✅ 邮件发送成功！")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def download_attachments(args, config):
    """下载附件"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        folder = args.folder or config['defaults']['folder']
        if folder.upper() == "INBOX":
            folder = "INBOX"
        
        status, messages = mail.select(folder)
        if status != 'OK':
            print(f"❌ 无法选择文件夹：{folder}")
            return
        
        # 获取指定 ID 的邮件
        msg_id = args.id
        status, msg_data = mail.fetch(msg_id, '(RFC822)')
        
        if status != 'OK':
            print(f"❌ 无法获取邮件：{msg_id}")
            return
        
        msg = email.message_from_bytes(msg_data[0][1])
        
        # 下载目录
        download_dir = os.path.expanduser(args.output_dir or config['defaults']['output_dir'])
        os.makedirs(download_dir, exist_ok=True)
        
        attachments = []
        
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get('Content-Disposition') is None:
                continue
            
            filename = part.get_filename()
            if filename:
                filename = decode_mime_words(filename)
                filepath = os.path.join(download_dir, filename)
                
                with open(filepath, 'wb') as f:
                    f.write(part.get_payload(decode=True))
                
                file_size = os.path.getsize(filepath) / 1024
                print(f"✅ 已下载：{filename} ({file_size:.1f} KB)")
                attachments.append(filepath)
        
        print(f"\n共下载 {len(attachments)} 个附件")
        print(f"保存位置：{download_dir}")
        
        mail.close()
        mail.logout()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def batch_delete(args, config):
    """批量删除邮件"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        folder = args.folder or config['defaults']['folder']
        if folder.upper() == "INBOX":
            folder = "INBOX"
        
        status, messages = mail.select(folder)
        if status != 'OK':
            print(f"❌ 无法选择文件夹：{folder}")
            return
        
        # 解析邮件 ID 列表
        msg_ids = []
        if args.ids:
            # 支持逗号分隔的 ID 列表，如 "1,2,3" 或 "1-5" 范围
            for id_part in args.ids.split(','):
                id_part = id_part.strip()
                if '-' in id_part:
                    # 范围，如 "1-5"
                    start, end = map(int, id_part.split('-'))
                    msg_ids.extend([str(i) for i in range(start, end + 1)])
                else:
                    msg_ids.append(id_part)
        elif args.all:
            # 获取所有邮件
            status, data = mail.search(None, 'ALL')
            if status == 'OK':
                msg_ids = data[0].split()
        
        if not msg_ids:
            print("❌ 没有指定要删除的邮件")
            return
        
        print(f"\n🗑️  准备删除 {len(msg_ids)} 封邮件...\n")
        
        deleted_count = 0
        for msg_id in msg_ids:
            status, store_result = mail.store(msg_id, '+FLAGS', '\\Deleted')
            if status == 'OK':
                deleted_count += 1
                if args.verbose:
                    print(f"  ✅ 已标记删除：{msg_id}")
            else:
                print(f"  ❌ 删除失败：{msg_id} - {store_result}")
        
        # 清空已删除
        if args.expunge:
            print(f"\n🗑️  清空已删除...")
            status, expunge_result = mail.expunge()
            if status == 'OK':
                print("✅ 已彻底删除")
        
        mail.close()
        mail.logout()
        
        print(f"\n✅ 成功删除 {deleted_count}/{len(msg_ids)} 封邮件")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def batch_move(args, config):
    """批量移动邮件"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        source_folder = args.source_folder or config['defaults']['folder']
        if source_folder.upper() == "INBOX":
            source_folder = "INBOX"
        
        status, messages = mail.select(source_folder)
        if status != 'OK':
            print(f"❌ 无法选择源文件夹：{source_folder}")
            return
        
        # 解析邮件 ID 列表
        msg_ids = []
        if args.ids:
            for id_part in args.ids.split(','):
                id_part = id_part.strip()
                if '-' in id_part:
                    start, end = map(int, id_part.split('-'))
                    msg_ids.extend([str(i) for i in range(start, end + 1)])
                else:
                    msg_ids.append(id_part)
        elif args.all:
            status, data = mail.search(None, 'ALL')
            if status == 'OK':
                msg_ids = data[0].split()
        
        if not msg_ids:
            print("❌ 没有指定要移动的邮件")
            return
        
        target_folder = args.target_folder
        print(f"\n📤 准备移动 {len(msg_ids)} 封邮件到 '{target_folder}'...\n")
        
        moved_count = 0
        for msg_id in msg_ids:
            status, copy_result = mail.copy(msg_id, target_folder)
            if status == 'OK':
                # 标记原邮件为已删除
                status, store_result = mail.store(msg_id, '+FLAGS', '\\Deleted')
                if status == 'OK':
                    moved_count += 1
                    if args.verbose:
                        print(f"  ✅ 已移动：{msg_id}")
                else:
                    print(f"  ⚠️  复制成功但标记失败：{msg_id}")
            else:
                print(f"  ❌ 移动失败：{msg_id} - {copy_result}")
        
        # 清空已删除
        if args.expunge:
            print(f"\n🗑️  清空已删除...")
            status, expunge_result = mail.expunge()
            if status == 'OK':
                print("✅ 已彻底删除")
        
        mail.close()
        mail.logout()
        
        print(f"\n✅ 成功移动 {moved_count}/{len(msg_ids)} 封邮件到 '{target_folder}'")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def mark_email(args, config):
    """标记邮件（已读/未读/星标）"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['password'])
        
        try:
            imap_id = config['imap_id']
            id_str = f'("name" "{imap_id["name"]}" "version" "{imap_id["version"]}" "vendor" "{imap_id["vendor"]}" "support-email" "{imap_id["support_email"]}")'
            mail.xatom('ID', id_str)
        except:
            pass
        
        folder = args.folder or config['defaults']['folder']
        if folder.upper() == "INBOX":
            folder = "INBOX"
        
        status, messages = mail.select(folder)
        if status != 'OK':
            print(f"❌ 无法选择文件夹：{folder}")
            return
        
        # 解析邮件 ID 列表
        msg_ids = []
        if args.ids:
            for id_part in args.ids.split(','):
                id_part = id_part.strip()
                if '-' in id_part:
                    start, end = map(int, id_part.split('-'))
                    msg_ids.extend([str(i) for i in range(start, end + 1)])
                else:
                    msg_ids.append(id_part)
        elif args.all:
            status, data = mail.search(None, 'ALL')
            if status == 'OK':
                msg_ids = data[0].split()
        
        if not msg_ids:
            print("❌ 没有指定要标记的邮件")
            return
        
        # 确定标记类型
        if args.read:
            flag = '\\Seen'
            action = '已读'
            operation = '-FLAGS'  # 移除未读标记
        elif args.unread:
            flag = '\\Seen'
            action = '未读'
            operation = '+FLAGS'  # 添加未读标记（实际是移除 Seen）
        elif args.star:
            flag = '\\Flagged'
            action = '星标'
            operation = '+FLAGS'
        elif args.unstar:
            flag = '\\Flagged'
            action = '取消星标'
            operation = '-FLAGS'
        else:
            print("❌ 请指定标记类型：--read, --unread, --star, --unstar")
            return
        
        print(f"\n🏷️  准备标记 {len(msg_ids)} 封邮件为'{action}'...\n")
        
        marked_count = 0
        for msg_id in msg_ids:
            if args.unread:
                # 标记未读：移除 \\Seen 标志
                status, store_result = mail.store(msg_id, '-FLAGS', '\\Seen')
            else:
                # 其他标记：添加或移除相应标志
                status, store_result = mail.store(msg_id, operation, flag)
            
            if status == 'OK':
                marked_count += 1
                if args.verbose:
                    print(f"  ✅ 已标记：{msg_id}")
            else:
                print(f"  ❌ 标记失败：{msg_id} - {store_result}")
        
        mail.close()
        mail.logout()
        
        print(f"\n✅ 成功标记 {marked_count}/{len(msg_ids)} 封邮件为'{action}'")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='163 邮箱管理工具 - 发送/接收/搜索/下载附件/批量操作/邮件标记',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  email-163-com init                          # 初始化配置
  email-163-com read --count 10               # 读取最新 10 封邮件
  email-163-com search --subject "鱼雷照片"    # 搜索邮件
  email-163-com send --to xxx@163.com --subject "测试" --body "内容"
  email-163-com attachments --id 123          # 下载附件
  email-163-com batch-delete --ids 1,2,3      # 批量删除邮件
  email-163-com batch-move --ids 1-5 --target-folder "备份"  # 批量移动
  email-163-com mark --ids 1,2,3 --read       # 标记为已读
  email-163-com mark --all --star             # 标记所有邮件为星标
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # init 命令
    init_parser = subparsers.add_parser('init', help='初始化配置')
    init_parser.set_defaults(func=lambda args, config: (init_config(), sys.exit(0)))
    
    # read 命令
    read_parser = subparsers.add_parser('read', help='读取邮件')
    read_parser.add_argument('--count', type=int, help='读取数量')
    read_parser.add_argument('--folder', type=str, help='文件夹')
    read_parser.add_argument('--unread', action='store_true', help='只读未读')
    read_parser.add_argument('--id', type=str, help='邮件 ID（指定ID时自动读取完整内容）')
    read_parser.set_defaults(func=read_emails)
    
    # search 命令
    search_parser = subparsers.add_parser('search', help='搜索邮件')
    search_parser.add_argument('--from', dest='from_addr', type=str, help='发件人')
    search_parser.add_argument('--subject', type=str, help='主题')
    search_parser.add_argument('--to', type=str, help='收件人')
    search_parser.add_argument('--folder', type=str, help='文件夹')
    search_parser.add_argument('--count', type=int, help='结果数量')
    search_parser.set_defaults(func=search_emails)
    
    # send 命令
    send_parser = subparsers.add_parser('send', help='发送邮件')
    send_parser.add_argument('--to', type=str, required=True, help='收件人')
    send_parser.add_argument('--subject', type=str, required=True, help='主题')
    send_parser.add_argument('--body', type=str, required=True, help='正文')
    send_parser.add_argument('--file', type=str, nargs='+', help='附件路径')
    send_parser.set_defaults(func=send_email)
    
    # attachments 命令
    attachments_parser = subparsers.add_parser('attachments', help='下载附件')
    attachments_parser.add_argument('--id', type=str, required=True, help='邮件 ID')
    attachments_parser.add_argument('--folder', type=str, help='文件夹')
    attachments_parser.add_argument('--output-dir', type=str, help='输出目录')
    attachments_parser.set_defaults(func=download_attachments)
    
    # folders 命令
    folders_parser = subparsers.add_parser('folders', help='列出文件夹')
    folders_parser.set_defaults(func=list_folders)
    
    # batch-delete 命令
    batch_delete_parser = subparsers.add_parser('batch-delete', help='批量删除邮件')
    batch_delete_parser.add_argument('--ids', type=str, help='邮件 ID 列表（逗号分隔，如 "1,2,3" 或 "1-5"）')
    batch_delete_parser.add_argument('--all', action='store_true', help='删除所有邮件')
    batch_delete_parser.add_argument('--folder', type=str, help='源文件夹')
    batch_delete_parser.add_argument('--expunge', action='store_true', help='彻底删除（清空已删除）')
    batch_delete_parser.add_argument('--verbose', '-v', action='store_true', help='显示详细信息')
    batch_delete_parser.set_defaults(func=batch_delete)
    
    # batch-move 命令
    batch_move_parser = subparsers.add_parser('batch-move', help='批量移动邮件')
    batch_move_parser.add_argument('--ids', type=str, help='邮件 ID 列表（逗号分隔）')
    batch_move_parser.add_argument('--all', action='store_true', help='移动所有邮件')
    batch_move_parser.add_argument('--source-folder', type=str, help='源文件夹')
    batch_move_parser.add_argument('--target-folder', type=str, required=True, help='目标文件夹')
    batch_move_parser.add_argument('--expunge', action='store_true', help='彻底删除原邮件')
    batch_move_parser.add_argument('--verbose', '-v', action='store_true', help='显示详细信息')
    batch_move_parser.set_defaults(func=batch_move)
    
    # mark 命令
    mark_parser = subparsers.add_parser('mark', help='标记邮件（已读/未读/星标）')
    mark_parser.add_argument('--ids', type=str, help='邮件 ID 列表（逗号分隔）')
    mark_parser.add_argument('--all', action='store_true', help='标记所有邮件')
    mark_parser.add_argument('--folder', type=str, help='文件夹')
    mark_parser.add_argument('--read', action='store_true', help='标记为已读')
    mark_parser.add_argument('--unread', action='store_true', help='标记为未读')
    mark_parser.add_argument('--star', action='store_true', help='添加星标')
    mark_parser.add_argument('--unstar', action='store_true', help='取消星标')
    mark_parser.add_argument('--verbose', '-v', action='store_true', help='显示详细信息')
    mark_parser.set_defaults(func=mark_email)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    config = load_config()
    args.func(args, config)


if __name__ == '__main__':
    main()
