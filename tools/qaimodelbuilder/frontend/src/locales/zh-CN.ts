// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// zh-CN message catalogue —— 主入口（纯组装），手工维护，UTF-8（无 BOM）。
//
// 本文件只负责把 ./zh-CN/{ns}.ts 各命名空间子文件组装成完整字典。
// 每个命名空间一个子文件，规则统一（无"大命名空间拆出 / 小命名空间内联"
// 的差别）。新增命名空间：在 ./zh-CN/ 下新建 {ns}.ts 并在此 import + 组装。
//
// 真值源与类型：en 主入口经 typeof 推导出 MessageSchema（见 ./schema.ts）；
// zh-CN / zh-TW 以 `: MessageSchema` 约束，由 tsc 强制三语 key 结构完全一致。
// 修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止 GBK/CP437 等损坏）。
// =============================================================================

import type { MessageSchema } from "./schema";
import aiCoding from "./zh-CN/aiCoding";
import app from "./zh-CN/app";
import appBuilder from "./zh-CN/appBuilder";
import appConfig from "./zh-CN/appConfig";
import auth from "./zh-CN/auth";
import channels from "./zh-CN/channels";
import chat from "./zh-CN/chat";
import chatErrors from "./zh-CN/chatErrors";
import chatHooks from "./zh-CN/chatHooks";
import claudeCode from "./zh-CN/claudeCode";
import cloudModels from "./zh-CN/cloudModels";
import codePersona from "./zh-CN/codePersona";
import commandPalette from "./zh-CN/commandPalette";
import common from "./zh-CN/common";
import config from "./zh-CN/config";
import depBroker from "./zh-CN/depBroker";
import downloads from "./zh-CN/downloads";
import error from "./zh-CN/error";
import execBroker from "./zh-CN/execBroker";
import feishu from "./zh-CN/feishu";
import favorites from "./zh-CN/favorites";
import fontSize from "./zh-CN/fontSize";
import forgeConfig from "./zh-CN/forgeConfig";
import harness from "./zh-CN/harness";
import help from "./zh-CN/help";
import index from "./zh-CN/index";
import input from "./zh-CN/input";
import language from "./zh-CN/language";
import layout from "./zh-CN/layout";
import mbPro from "./zh-CN/mbPro";
import mcpServers from "./zh-CN/mcpServers";
import modeIntro from "./zh-CN/modeIntro";
import modelBuilder from "./zh-CN/modelBuilder";
import modelHubFrame from "./zh-CN/modelHubFrame";
import models from "./zh-CN/models";
import nav from "./zh-CN/nav";
import openCode from "./zh-CN/openCode";
import policyTemplates from "./zh-CN/policyTemplates";
import projectAccess from "./zh-CN/projectAccess";
import promptEnhance from "./zh-CN/promptEnhance";
import promptHistory from "./zh-CN/promptHistory";
import promptSnapshot from "./zh-CN/promptSnapshot";
import reboot from "./zh-CN/reboot";
import renameDialog from "./zh-CN/renameDialog";
import security from "./zh-CN/security";
import service from "./zh-CN/service";
import serviceConfig from "./zh-CN/serviceConfig";
import sessionWorkspace from "./zh-CN/sessionWorkspace";
import settings from "./zh-CN/settings";
import sidebar from "./zh-CN/sidebar";
import simulator from "./zh-CN/simulator";
import skills from "./zh-CN/skills";
import status from "./zh-CN/status";
import theme from "./zh-CN/theme";
import time from "./zh-CN/time";
import toast from "./zh-CN/toast";
import tool from "./zh-CN/tool";
import toolbar from "./zh-CN/toolbar";
import toolSafety from "./zh-CN/toolSafety";
import util from "./zh-CN/util";
import views from "./zh-CN/views";
import voiceInput from "./zh-CN/voiceInput";
import wechat from "./zh-CN/wechat";
import userMessageJump from "./zh-CN/userMessageJump";

const zh_CN: MessageSchema = {
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

export default zh_CN;
