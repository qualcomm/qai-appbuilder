// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// zh-TW message catalogue —— 主入口（纯组装），手工维护，UTF-8（无 BOM）。
//
// 本文件只负责把 ./zh-TW/{ns}.ts 各命名空间子文件组装成完整字典。
// 每个命名空间一个子文件，规则统一（无"大命名空间拆出 / 小命名空间内联"
// 的差别）。新增命名空间：在 ./zh-TW/ 下新建 {ns}.ts 并在此 import + 组装。
//
// 真值源与类型：en 主入口经 typeof 推导出 MessageSchema（见 ./schema.ts）；
// zh-CN / zh-TW 以 `: MessageSchema` 约束，由 tsc 强制三语 key 结构完全一致。
// 修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止 GBK/CP437 等损坏）。
// =============================================================================

import type { MessageSchema } from "./schema";
import aiCoding from "./zh-TW/aiCoding";
import app from "./zh-TW/app";
import appBuilder from "./zh-TW/appBuilder";
import appConfig from "./zh-TW/appConfig";
import auth from "./zh-TW/auth";
import channels from "./zh-TW/channels";
import chat from "./zh-TW/chat";
import chatErrors from "./zh-TW/chatErrors";
import chatHooks from "./zh-TW/chatHooks";
import claudeCode from "./zh-TW/claudeCode";
import cloudModels from "./zh-TW/cloudModels";
import codePersona from "./zh-TW/codePersona";
import commandPalette from "./zh-TW/commandPalette";
import common from "./zh-TW/common";
import config from "./zh-TW/config";
import depBroker from "./zh-TW/depBroker";
import downloads from "./zh-TW/downloads";
import error from "./zh-TW/error";
import execBroker from "./zh-TW/execBroker";
import feishu from "./zh-TW/feishu";
import favorites from "./zh-TW/favorites";
import fontSize from "./zh-TW/fontSize";
import forgeConfig from "./zh-TW/forgeConfig";
import harness from "./zh-TW/harness";
import help from "./zh-TW/help";
import index from "./zh-TW/index";
import input from "./zh-TW/input";
import language from "./zh-TW/language";
import layout from "./zh-TW/layout";
import mbPro from "./zh-TW/mbPro";
import mcpServers from "./zh-TW/mcpServers";
import modeIntro from "./zh-TW/modeIntro";
import modelBuilder from "./zh-TW/modelBuilder";
import modelHubFrame from "./zh-TW/modelHubFrame";
import models from "./zh-TW/models";
import nav from "./zh-TW/nav";
import openCode from "./zh-TW/openCode";
import policyTemplates from "./zh-TW/policyTemplates";
import projectAccess from "./zh-TW/projectAccess";
import promptEnhance from "./zh-TW/promptEnhance";
import promptHistory from "./zh-TW/promptHistory";
import promptSnapshot from "./zh-TW/promptSnapshot";
import reboot from "./zh-TW/reboot";
import renameDialog from "./zh-TW/renameDialog";
import security from "./zh-TW/security";
import service from "./zh-TW/service";
import serviceConfig from "./zh-TW/serviceConfig";
import sessionWorkspace from "./zh-TW/sessionWorkspace";
import settings from "./zh-TW/settings";
import sidebar from "./zh-TW/sidebar";
import simulator from "./zh-TW/simulator";
import skills from "./zh-TW/skills";
import status from "./zh-TW/status";
import theme from "./zh-TW/theme";
import time from "./zh-TW/time";
import toast from "./zh-TW/toast";
import tool from "./zh-TW/tool";
import toolbar from "./zh-TW/toolbar";
import toolSafety from "./zh-TW/toolSafety";
import util from "./zh-TW/util";
import views from "./zh-TW/views";
import voiceInput from "./zh-TW/voiceInput";
import wechat from "./zh-TW/wechat";
import userMessageJump from "./zh-TW/userMessageJump";

const zh_TW: MessageSchema = {
  aiCoding,
  app,
  appBuilder,
  appConfig,
  auth,
  channels,
  chat,
  chatErrors,
  chatHooks,
  claudeCode,
  cloudModels,
  codePersona,
  commandPalette,
  common,
  config,
  depBroker,
  downloads,
  error,
  execBroker,
  feishu,
  favorites,
  fontSize,
  forgeConfig,
  harness,
  help,
  index,
  input,
  language,
  layout,
  mbPro,
  mcpServers,
  modeIntro,
  modelBuilder,
  modelHubFrame,
  models,
  nav,
  openCode,
  policyTemplates,
  projectAccess,
  promptEnhance,
  promptHistory,
  promptSnapshot,
  reboot,
  renameDialog,
  security,
  service,
  serviceConfig,
  sessionWorkspace,
  settings,
  sidebar,
  simulator,
  skills,
  status,
  theme,
  time,
  toast,
  tool,
  toolbar,
  toolSafety,
  util,
  views,
  voiceInput,
  wechat,
  userMessageJump,
};

export default zh_TW;
