//! Shell texts. Chosen from the Windows UI language once at start (zh -> Simplified Chinese, else English).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Lang {
    Zh,
    En,
}

pub fn detect() -> Lang {
    #[cfg(windows)]
    {
        use windows::Win32::Globalization::GetUserDefaultUILanguage;
        // primary language id is the low 10 bits; 0x04 = Chinese
        let id = unsafe { GetUserDefaultUILanguage() };
        if id & 0x3ff == 0x04 {
            return Lang::Zh;
        }
    }
    Lang::En
}

pub struct T {
    pub app_title: &'static str,
    pub tray_show: &'static str,
    pub tray_quit: &'static str,
    pub tray_tip: &'static str,
    pub close_title: &'static str,
    pub close_main: &'static str,
    pub close_content: &'static str,
    pub btn_minimize: &'static str,
    pub btn_minimize_note: &'static str,
    pub btn_quit: &'static str,
    pub btn_quit_note: &'static str,
    pub remember: &'static str,
    pub splash_starting: &'static str,
    pub splash_failed: &'static str,
    pub err_no_root: &'static str,
    pub err_no_venv: &'static str,
    pub err_port_busy: &'static str,
    pub err_spawn: &'static str,
    pub err_exited: &'static str,
    pub err_timeout: &'static str,
}

pub fn texts(l: Lang) -> &'static T {
    match l {
        Lang::Zh => &ZH,
        Lang::En => &EN,
    }
}

static ZH: T = T {
    app_title: "QAI ModelBuilder",
    tray_show: "显示窗口",
    tray_quit: "退出",
    tray_tip: "QAI ModelBuilder(后台运行中)",
    close_title: "QAI ModelBuilder",
    close_main: "要关闭 QAI ModelBuilder 吗?",
    close_content: "退出会停止后台服务;最小化到托盘则服务继续运行,可从系统托盘重新打开。",
    btn_minimize: "最小化到托盘",
    btn_minimize_note: "服务继续在后台运行",
    btn_quit: "退出程序",
    btn_quit_note: "停止服务并关闭窗口",
    remember: "记住我的选择,下次不再询问",
    splash_starting: "正在启动服务…",
    splash_failed: "启动失败",
    err_no_root: "找不到安装目录(应包含 src、apps、factory 文件夹)。请重新安装,或把本程序放回安装目录。",
    err_no_venv: "找不到 Python 运行环境。请先运行安装目录中的 setup_env.bat(或使用「修复」快捷方式)。",
    err_port_busy: "端口 {port} 已被占用。请关闭浏览器版(Start.bat)或其他占用该端口的程序后重试。",
    err_spawn: "无法启动后台服务",
    err_exited: "后台服务意外退出",
    err_timeout: "后台服务启动超时",
};

static EN: T = T {
    app_title: "QAI ModelBuilder",
    tray_show: "Show window",
    tray_quit: "Quit",
    tray_tip: "QAI ModelBuilder (running in background)",
    close_title: "QAI ModelBuilder",
    close_main: "Close QAI ModelBuilder?",
    close_content: "Quitting stops the background service; minimizing to the tray keeps it running and you can reopen the window from the system tray.",
    btn_minimize: "Minimize to tray",
    btn_minimize_note: "The service keeps running in the background",
    btn_quit: "Quit",
    btn_quit_note: "Stop the service and close the window",
    remember: "Remember my choice and don't ask again",
    splash_starting: "Starting the service...",
    splash_failed: "Failed to start",
    err_no_root: "Cannot find the install folder (it must contain src, apps and factory). Reinstall, or put this program back into the install folder.",
    err_no_venv: "The Python environment was not found. Run setup_env.bat in the install folder first (or use the Repair shortcut).",
    err_port_busy: "Port {port} is already in use. Close the browser edition (Start.bat) or whatever uses the port, then try again.",
    err_spawn: "Could not start the background service",
    err_exited: "The background service exited unexpectedly",
    err_timeout: "The background service did not become ready in time",
};

