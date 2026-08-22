-- ---------------------------------------------------------------------
-- Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
-- SPDX-License-Identifier: BSD-3-Clause
-- ---------------------------------------------------------------------
--
-- 加速 lifespan 启动扫描'未整合 SYSTEM_NOTICE'派生逻辑（Batch 10 N.42 使用）。
-- 076 已把 role='system_notice' 加入 CHECK；本 migration 补一个 partial index。
--
-- 编号说明：本文件原为 077，因 077_create_data_migrations_table（随 PR #267
-- 进入 main）占用了该号而后移。migrations.py 的 _validate_no_gaps 既禁止重号
-- 也要求连号，且 Migration.id 由版本号+名字拼成并写入 _qai_schema_migrations，
-- 所以已发布的那一侧不能改名 —— 只能由本地未发布的迁移让位。改名后本迁移会在
-- 已有开发库上以新 id 重跑一次：CREATE INDEX IF NOT EXISTS 使其为 no-op。
--
-- Partial (WHERE role = 'system_notice') so it stays tiny: notice rows are a
-- small minority of chat_message, and every non-notice INSERT skips the index
-- entirely.  Keyed (conversation_id, position) because the startup scan reads
-- one conversation's notices in transcript order.

CREATE INDEX IF NOT EXISTS ix_chat_message_role_system_notice
    ON chat_message(conversation_id, position)
    WHERE role = 'system_notice';
