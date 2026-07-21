// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// en message catalogue —— 主入口（纯组装），手工维护，UTF-8（无 BOM）。
//
// 本文件只负责把 ./en/{ns}.ts 各命名空间子文件组装成完整字典。
// 每个命名空间一个子文件，规则统一（无"大命名空间拆出 / 小命名空间内联"
// 的差别）。新增命名空间：在 ./en/ 下新建 {ns}.ts 并在此 import + 组装。
//
// 真值源与类型：en 主入口经 typeof 推导出 MessageSchema（见 ./schema.ts）；
// zh-CN / zh-TW 以 `: MessageSchema` 约束，由 tsc 强制三语 key 结构完全一致。
// 修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止 GBK/CP437 等损坏）。
// =============================================================================

import aiCoding from "./en/aiCoding";
import app from "./en/app";
import appBuilder from "./en/appBuilder";
import appConfig from "./en/appConfig";
import auth from "./en/auth";
import channels from "./en/channels";
import chat from "./en/chat";
import chatErrors from "./en/chatErrors";
import chatHooks from "./en/chatHooks";
import claudeCode from "./en/claudeCode";
import cloudModels from "./en/cloudModels";
import codePersona from "./en/codePersona";
import commandPalette from "./en/commandPalette";
import common from "./en/common";
import config from "./en/config";
import depBroker from "./en/depBroker";
import downloads from "./en/downloads";
import error from "./en/error";
import execBroker from "./en/execBroker";
import feishu from "./en/feishu";
import favorites from "./en/favorites";
import fontSize from "./en/fontSize";
import forgeConfig from "./en/forgeConfig";
import harness from "./en/harness";
import help from "./en/help";
import index from "./en/index";
import input from "./en/input";
import language from "./en/language";
import layout from "./en/layout";
import mbPro from "./en/mbPro";
import mcpServers from "./en/mcpServers";
import modeIntro from "./en/modeIntro";
import modelBuilder from "./en/modelBuilder";
import modelHubFrame from "./en/modelHubFrame";
import models from "./en/models";
import nav from "./en/nav";
import openCode from "./en/openCode";
import policyTemplates from "./en/policyTemplates";
import projectAccess from "./en/projectAccess";
import promptEnhance from "./en/promptEnhance";
import promptHistory from "./en/promptHistory";
import promptSnapshot from "./en/promptSnapshot";
import reboot from "./en/reboot";
import renameDialog from "./en/renameDialog";
import security from "./en/security";
import service from "./en/service";
import serviceConfig from "./en/serviceConfig";
import sessionWorkspace from "./en/sessionWorkspace";
import settings from "./en/settings";
import sidebar from "./en/sidebar";
import simulator from "./en/simulator";
import skills from "./en/skills";
import status from "./en/status";
import theme from "./en/theme";
import time from "./en/time";
import toast from "./en/toast";
import tool from "./en/tool";
import toolbar from "./en/toolbar";
import toolSafety from "./en/toolSafety";
import util from "./en/util";
import views from "./en/views";
import voiceInput from "./en/voiceInput";
import wechat from "./en/wechat";
import userMessageJump from "./en/userMessageJump";

const en = {
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

export default en;
