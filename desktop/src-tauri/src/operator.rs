use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    path::Path,
    process::{Command, Stdio},
    sync::OnceLock,
    thread,
    time::Instant,
};

use regex::Regex;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Preset {
    Readonly,
    TopicWriter,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Status {},
    Install { port: u16 },
    Stop {},
    Import {},
    Attach { preset: Preset },
    SourceStatus {},
    SourceConfigure { token: String },
    UninstallPreserve {},
    UninstallPurge { confirmation: String },
}

pub enum Invocation {
    Status,
    Install(u16),
    Stop,
    Import,
    Attach(Preset),
    SourceStatus,
    SourceConfigure(String),
    UninstallPreserve,
    UninstallPurge,
}

const PURGE_CONFIRMATION: &str = "DELETE-THIS-INSTANCE";

impl TryFrom<Operation> for Invocation {
    type Error = String;

    fn try_from(operation: Operation) -> Result<Self, Self::Error> {
        Ok(match operation {
            Operation::Status {} => Self::Status,
            Operation::Install { port } if port >= 1024 => Self::Install(port),
            Operation::Install { .. } => return Err("请输入 1024 至 65535 的端口。".into()),
            Operation::Stop {} => Self::Stop,
            Operation::Import {} => Self::Import,
            Operation::Attach { preset } => Self::Attach(preset),
            Operation::SourceStatus {} => Self::SourceStatus,
            Operation::SourceConfigure { token }
                if !token.is_empty()
                    && token.len() <= 256
                    && token.bytes().all(|byte| byte.is_ascii_graphic()) =>
            {
                Self::SourceConfigure(token)
            }
            Operation::SourceConfigure { .. } => return Err("请输入有效的 Tushare Token。".into()),
            Operation::UninstallPreserve {} => Self::UninstallPreserve,
            Operation::UninstallPurge { confirmation } if confirmation == PURGE_CONFIRMATION => {
                Self::UninstallPurge
            }
            Operation::UninstallPurge { .. } => return Err("请逐字输入卸载确认文字。".into()),
        })
    }
}

impl Invocation {
    pub fn closes_workbench(&self) -> bool {
        matches!(
            self,
            Self::Stop | Self::UninstallPreserve | Self::UninstallPurge
        )
    }

    fn args(&self) -> Vec<String> {
        let words: &[&str] = match self {
            Self::Status => &["desktop-status"],
            Self::Install(_) => &["desktop-install", "--port"],
            Self::Stop => &["desktop-stop"],
            Self::Import => &["desktop-import"],
            Self::Attach(Preset::Readonly) => &["desktop-attach", "--preset", "readonly"],
            Self::Attach(Preset::TopicWriter) => &["desktop-attach", "--preset", "topic-writer"],
            Self::SourceStatus => &["desktop-source-status"],
            Self::SourceConfigure(_) => &["desktop-source-configure"],
            Self::UninstallPreserve => &["desktop-uninstall", "--mode", "preserve"],
            Self::UninstallPurge => &[
                "desktop-uninstall",
                "--mode",
                "purge",
                "--confirm",
                PURGE_CONFIRMATION,
            ],
        };
        let mut args: Vec<_> = words.iter().map(|word| (*word).to_owned()).collect();
        if let Self::Install(port) = self {
            args.push(port.to_string());
        }
        args
    }

    fn source_diagnostic(&self) -> bool {
        matches!(self, Self::SourceStatus | Self::SourceConfigure(_))
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Status {
    pub docker: String,
    pub installed: bool,
    pub has_instance: bool,
    pub phase: String,
    pub running: bool,
    pub port: u16,
    pub preset: Option<String>,
    #[serde(default)]
    pub scopes: Vec<String>,
    #[serde(default)]
    pub pending_revocation: bool,
    pub credential_active: Option<bool>,
    pub repair: Option<String>,
    pub project: Option<String>,
    #[serde(default)]
    pub volumes: Vec<String>,
}

#[derive(Clone, Serialize)]
pub struct Progress {
    pub stream: &'static str,
    pub line: String,
}

#[derive(Serialize)]
pub struct OperationResult {
    pub exit_code: i32,
    pub elapsed_seconds: f64,
    pub command: String,
    pub status: Option<Status>,
    pub source: Option<SourceDiagnostic>,
}

#[derive(Deserialize, Serialize)]
pub struct SourceDiagnostic {
    pub saved: bool,
    pub restart: SourceRestart,
    pub capabilities: BTreeMap<SourceCapability, SourceCheck>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceRestart {
    NotRequested,
    Ready,
    Failed,
}

#[derive(Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceCapability {
    Daily,
    AdjFactor,
    IndexDaily,
    DailyBasic,
    FinaIndicator,
    TradeCal,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceCheck {
    Available,
    CredentialsMissing,
    InvalidCredentials,
    PermissionDenied,
    QuotaExhausted,
    ConnectionFailed,
    CoverageInsufficient,
}

#[derive(Clone, Copy)]
enum OutputKind {
    Log,
    Status,
    Source,
    Silent,
}

#[derive(Default)]
struct ParsedOutput {
    status: Option<Status>,
    source: Option<SourceDiagnostic>,
}

fn safe_line(raw: &str) -> String {
    static SENSITIVE: OnceLock<Regex> = OnceLock::new();
    let sensitive = SENSITIVE.get_or_init(|| {
        Regex::new(concat!(
            r"(?i)\bfbmc_MC-[a-f0-9]+_[a-z0-9_-]+",
            r"|\bbearer\s+[a-z0-9._~+/=-]+",
            r#"|(?:token|pass(?:word)?|authorization|credential|secret|api[_-]?key)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,}]+)"#,
            r"|://[^\s/@]+:[^\s/@]+@"
        ))
        .expect("redaction pattern must be valid")
    });
    sensitive
        .replace_all(raw, "[已脱敏]")
        .chars()
        .filter(|ch| !ch.is_control() || *ch == '\t')
        .take(1000)
        .collect()
}

fn read_output(
    pipe: impl Read,
    stream: &'static str,
    kind: OutputKind,
    emit: &(impl Fn(Progress) + Sync),
) -> ParsedOutput {
    let mut output = ParsedOutput::default();
    for (index, result) in BufReader::new(pipe).split(b'\n').enumerate() {
        let Ok(line) = result else { break };
        let decoded = String::from_utf8_lossy(&line);
        let line = decoded.trim().trim_start_matches('\u{feff}');
        if line.is_empty() {
            continue;
        }
        if matches!(kind, OutputKind::Silent) {
            continue;
        }
        if matches!(kind, OutputKind::Source) {
            if let Ok(diagnostic) = serde_json::from_str(line) {
                output.source = Some(diagnostic);
            }
            continue;
        }
        if matches!(kind, OutputKind::Status) {
            if let Ok(parsed) = serde_json::from_str::<Status>(line) {
                output.status = Some(Status {
                    repair: parsed.repair.as_deref().map(safe_line),
                    ..parsed
                });
                continue;
            }
        }
        if index < 240 {
            emit(Progress {
                stream,
                line: safe_line(line),
            });
        }
    }
    output
}

fn equivalent_command(script: &Path, args: &[String]) -> String {
    format!(
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File '{}' {}",
        script.to_string_lossy().replace('\'', "''"),
        args.join(" ")
    )
}

fn powershell() -> Result<std::path::PathBuf, String> {
    let system = std::env::var_os("SystemRoot").ok_or("无法定位 Windows PowerShell。")?;
    Ok(Path::new(&system).join("System32/WindowsPowerShell/v1.0/powershell.exe"))
}

pub(crate) fn open_in_default_browser(url: &tauri::Url) -> Result<(), String> {
    let mut launcher = Command::new(powershell()?);
    launcher
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Process -FilePath $env:FINBOARD_DESKTOP_WORKBENCH_URL -ErrorAction Stop",
        ])
        .env("FINBOARD_DESKTOP_WORKBENCH_URL", url.as_str())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        launcher.creation_flags(0x0800_0000);
    }
    if launcher.status().is_ok_and(|exit_code| exit_code.success()) {
        Ok(())
    } else {
        Err("无法打开系统浏览器，请检查默认浏览器，或复制工作台地址。".into())
    }
}

pub fn execute(
    script: &Path,
    invocation: &Invocation,
    emit: impl Fn(Progress) + Sync,
) -> Result<OperationResult, String> {
    // Tauri 可能返回扩展长度路径，Windows PowerShell 的文件提供程序不能用它解析 $PSScriptRoot。
    let script = dunce::simplified(script);
    let arguments = invocation.args();
    let started = Instant::now();
    let mut command = Command::new(powershell()?);
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        .arg(script)
        .args(&arguments)
        .env_remove("FINBOARD_DESKTOP_TEST_ROOT")
        .env_remove("FINBOARD_DESKTOP_TEST_PROJECT")
        .stdin(if matches!(invocation, Invocation::SourceConfigure(_)) {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|error| {
        format!(
            "无法运行 Windows PowerShell。{}",
            safe_line(&error.to_string())
        )
    })?;
    let stdout = child.stdout.take().expect("stdout is piped");
    let stderr = child.stderr.take().expect("stderr is piped");
    let parse_status = matches!(invocation, Invocation::Status);
    let parse_source = invocation.source_diagnostic();
    let stdout_kind = if parse_source {
        OutputKind::Source
    } else if parse_status {
        OutputKind::Status
    } else {
        OutputKind::Log
    };
    let stderr_kind = if parse_source {
        OutputKind::Silent
    } else {
        OutputKind::Log
    };
    let (exit, output) = thread::scope(|scope| {
        let output = scope.spawn(|| read_output(stdout, "stdout", stdout_kind, &emit));
        let errors = scope.spawn(|| read_output(stderr, "stderr", stderr_kind, &emit));
        if let Invocation::SourceConfigure(token) = invocation {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(token.as_bytes());
            }
        }
        let exit = child.wait();
        let output = output.join().unwrap_or_default();
        let _ = errors.join();
        (exit, output)
    });
    let exit_code = exit
        .map_err(|_| "无法读取主机操作退出码。")?
        .code()
        .unwrap_or(-1);
    if parse_status && exit_code == 0 && output.status.is_none() {
        return Err("安装器没有返回可识别的状态，请重新安装同版本 MSI。".into());
    }
    if parse_source && output.source.is_none() {
        return Err("无法确认 Tushare 操作结果。请检查服务是否就绪，再检查权限。".into());
    }
    Ok(OperationResult {
        exit_code,
        elapsed_seconds: started.elapsed().as_secs_f64(),
        command: equivalent_command(script, &arguments),
        status: output.status,
        source: output.source,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn powershell_resolves_resource_root_from_extended_install_path() {
        use std::fs;

        let directory =
            std::env::temp_dir().join(format!("finboard operator {}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let script = directory.join("verify.ps1");
        fs::write(
            &script,
            "$ErrorActionPreference = 'Stop'\n$fixture = Join-Path $PSScriptRoot 'verify.ps1'\nif (!(Test-Path -LiteralPath $fixture)) { exit 1 }\n",
        )
        .unwrap();
        let extended = fs::canonicalize(&script).unwrap();
        assert_ne!(&extended, dunce::simplified(&extended));
        let result = execute(&extended, &Invocation::Stop, |_| {});
        fs::remove_file(script).unwrap();
        fs::remove_dir(directory).unwrap();
        let result = result.unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(!result.command.contains(r"\\?\"));
    }

    #[test]
    fn operations_only_produce_fixed_arguments() {
        assert!(
            serde_json::from_str::<Operation>(r#"{"kind":"shell","command":"docker"}"#).is_err()
        );
        assert!(serde_json::from_str::<Operation>(r#"{"kind":"status","port":1234}"#).is_err());
        assert!(Invocation::try_from(Operation::Install { port: 80 }).is_err());
        assert!(Invocation::try_from(Operation::UninstallPurge {
            confirmation: "yes".into()
        })
        .is_err());
        let attach = Invocation::try_from(Operation::Attach {
            preset: Preset::TopicWriter,
        })
        .unwrap();
        assert_eq!(
            attach.args(),
            ["desktop-attach", "--preset", "topic-writer"]
        );
        assert!(
            equivalent_command(Path::new("C:/option 'test'/finboard.ps1"), &attach.args())
                .contains("'C:/option ''test''/finboard.ps1'")
        );
    }

    #[test]
    fn source_credential_is_never_returned_as_part_of_diagnostics() {
        let token = "tushare-fixture-not-a-real-token";
        let invocation = Invocation::try_from(Operation::SourceConfigure {
            token: token.into(),
        })
        .unwrap();
        assert_eq!(invocation.args(), ["desktop-source-configure"]);
        assert!(Invocation::try_from(Operation::SourceConfigure {
            token: "first\nsecond".into(),
        })
        .is_err());

        let report = format!(
            "{}\n{}",
            token,
            r#"{"saved":false,"restart":"not_requested","capabilities":{"daily":"invalid_credentials"}}"#
        );
        let output = read_output(report.as_bytes(), "stdout", OutputKind::Source, &|_| {
            panic!("source diagnostics must not return log lines")
        });
        let value = serde_json::to_string(&output.source.unwrap()).unwrap();
        assert!(value.contains("invalid_credentials"));
        assert!(!value.contains(token));
        let _ = read_output(token.as_bytes(), "stderr", OutputKind::Silent, &|_| {
            panic!("source diagnostics must not return stderr lines")
        });
    }

    #[test]
    fn log_redaction_keeps_operational_context() {
        for input in [
            "Bearer VALUENOTTOLOG",
            "fbmc_MC-0123456789ab_VALUENOTTOLOG",
            "postgresql://user:VALUENOTTOLOG@localhost/db",
            "\"password\": \"VALUENOTTOLOG\"",
            "token=VALUENOTTOLOG",
        ] {
            let line = safe_line(&format!("Docker check failed: {input}"));
            assert_eq!(line.find("Docker check failed"), Some(0));
            assert!(!line.contains("VALUENOTTOLOG"));
        }
    }
}
