//! The backend process: spawn `python -m apps.cli.serve --port <p>` hidden, inside a kill-on-close Job Object
//! (so it cannot outlive the shell even if the shell is killed), and stop it gracefully through
//! `POST /api/system/exit` (CSRF double-submit: GET the cookie, send it back as `X-QAI-CSRF`).
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub struct Backend {
    pub child: Child,
    pub port: u16,
    #[cfg(windows)]
    _job: Job,
}

#[cfg(windows)]
struct Job(windows::Win32::Foundation::HANDLE);
// The handle is only closed on drop; nothing else touches it.
#[cfg(windows)]
unsafe impl Send for Job {}
#[cfg(windows)]
unsafe impl Sync for Job {}

#[cfg(windows)]
impl Drop for Job {
    fn drop(&mut self) {
        // KILL_ON_JOB_CLOSE: closing the last handle terminates every process still in the job.
        unsafe {
            let _ = windows::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
fn make_job() -> Option<Job> {
    use windows::Win32::System::JobObjects::*;
    unsafe {
        let h = CreateJobObjectW(None, None).ok()?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            h,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .ok()?;
        Some(Job(h))
    }
}

/// Is something already listening on 127.0.0.1:port or [::1]:port?
pub fn port_in_use(port: u16) -> bool {
    let t = Duration::from_millis(400);
    let v4: SocketAddr = ([127, 0, 0, 1], port).into();
    let v6: SocketAddr = (std::net::Ipv6Addr::LOCALHOST, port).into();
    TcpStream::connect_timeout(&v4, t).is_ok() || TcpStream::connect_timeout(&v6, t).is_ok()
}

pub fn spawn(root: &Path, python: &Path, port: u16) -> Result<Backend, String> {
    let mut cmd = Command::new(python);
    cmd.args(["-m", "apps.cli.serve", "--port", &port.to_string()])
        .current_dir(root)
        .env("PYTHONPATH", "src;.")
        .env("PYTHONPYCACHEPREFIX", root.join("data").join("caches").join("pycache"))
        .stdin(Stdio::null());
    // Keep the backend output in a log file: the release shell has no console.
    let logs = crate::layout::log_dir(root);
    let _ = std::fs::create_dir_all(&logs);
    match std::fs::OpenOptions::new().create(true).append(true).open(logs.join("desktop-backend.log")) {
        Ok(f) => {
            if let Ok(f2) = f.try_clone() {
                cmd.stderr(Stdio::from(f2));
            }
            cmd.stdout(Stdio::from(f));
        }
        Err(_) => {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: no console window; the supervisor's own Ctrl+C handling
        // is not triggered by the shell.
        cmd.creation_flags(0x0800_0000 | 0x0000_0200);
    }
    let child = cmd.spawn().map_err(|e| format!("{e}"))?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        let job = make_job().ok_or_else(|| "could not create the job object".to_string())?;
        unsafe {
            use windows::Win32::Foundation::HANDLE;
            use windows::Win32::System::JobObjects::AssignProcessToJobObject;
            AssignProcessToJobObject(job.0, HANDLE(child.as_raw_handle() as _)).map_err(|e| format!("{e}"))?;
        }
        return Ok(Backend { child, port, _job: job });
    }
    #[cfg(not(windows))]
    Ok(Backend { child, port })
}

/// One HTTP request over a plain socket (the shell has no HTTP client dependency). Returns (status, headers, body).
fn http(port: u16, method: &str, path: &str, extra: &str, timeout: Duration) -> Option<(u16, String, String)> {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut s = TcpStream::connect_timeout(&addr, timeout).ok()?;
    s.set_read_timeout(Some(timeout)).ok()?;
    s.set_write_timeout(Some(timeout)).ok()?;
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nContent-Length: 0\r\n{extra}\r\n"
    );
    s.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf).into_owned();
    let (head, body) = text.split_once("\r\n\r\n")?;
    let status = head.lines().next()?.split_whitespace().nth(1)?.parse().ok()?;
    Some((status, head.to_string(), body.to_string()))
}

/// `/api/system/health` answers 200 (the backend finished its lifespan start-up).
pub fn healthy(port: u16) -> bool {
    matches!(http(port, "GET", "/api/system/health", "", Duration::from_millis(1500)), Some((200, _, _)))
}

/// Has the supervisor process exited? `Some(code)` when it has.
pub fn exited(b: &mut Backend) -> Option<Option<i32>> {
    match b.child.try_wait() {
        Ok(Some(st)) => Some(st.code()),
        _ => None,
    }
}
/// Graceful stop: POST /api/system/exit (needs the CSRF cookie), wait for the supervisor, then force the tree.
pub fn stop(b: &mut Backend) {
    if matches!(b.child.try_wait(), Ok(Some(_))) {
        return;
    }
    let t = Duration::from_millis(2500);
    let mut ok = false;
    if let Some((_, head, _)) = http(b.port, "GET", "/api/system/health", "", t) {
        // Set-Cookie: qai_csrf=<token>; ...
        let token = head
            .lines()
            .filter(|l| l.to_ascii_lowercase().starts_with("set-cookie:"))
            .find_map(|l| l.split("qai_csrf=").nth(1).map(|r| r.split(';').next().unwrap_or("").trim().to_string()));
        if let Some(tok) = token.filter(|t| !t.is_empty()) {
            let extra = format!("Cookie: qai_csrf={tok}\r\nX-QAI-CSRF: {tok}\r\n");
            ok = matches!(http(b.port, "POST", "/api/system/exit", &extra, t), Some((s, _, _)) if s < 300);
        }
    }
    let deadline = Instant::now() + Duration::from_secs(if ok { 15 } else { 1 });
    while Instant::now() < deadline {
        if matches!(b.child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    kill_tree(b);
}

/// Last resort: kill the supervisor and everything under it (taskkill /T), then the job handle drops with `b`.
pub fn kill_tree(b: &mut Backend) {
    let pid = b.child.id();
    let mut c = Command::new("taskkill");
    c.args(["/T", "/F", "/PID", &pid.to_string()]).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000);
    }
    let _ = c.status();
    let _ = b.child.kill();
    let _ = b.child.wait();
}

