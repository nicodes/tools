use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{DirBuilder, OpenOptions},
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    os::unix::process::CommandExt,
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
pub const FILES: &[(&str, &[u8])] = &[
    (
        "fleet_operator.py",
        include_bytes!("../internal/operator/assets/fleet_operator.py"),
    ),
    (
        "fleet_broker.py",
        include_bytes!("../internal/operator/assets/fleet_broker.py"),
    ),
    (
        "fleet_publisher.py",
        include_bytes!("../internal/operator/assets/fleet_publisher.py"),
    ),
    (
        "fleet-operator.service",
        include_bytes!("../internal/operator/assets/fleet-operator.service"),
    ),
    (
        "fleet-operator.socket",
        include_bytes!("../internal/operator/assets/fleet-operator.socket"),
    ),
    (
        "fleet-operator.conf",
        include_bytes!("../internal/operator/assets/fleet-operator.conf"),
    ),
];
pub fn plan() -> BTreeMap<&'static str, String> {
    FILES
        .iter()
        .map(|(name, body)| {
            (
                *name,
                Sha256::digest(body)
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>(),
            )
        })
        .collect()
}
pub fn export(directory: &Path) -> Result<()> {
    let canonical: PathBuf = directory.components().collect();
    if !directory.is_absolute()
        || directory == Path::new("/")
        || canonical.as_os_str() != directory.as_os_str()
        || directory
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        return Err("operator export requires a new absolute canonical directory".into());
    }
    DirBuilder::new().mode(0o700).create(directory)?;
    for (name, body) in FILES {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(directory.join(name))?;
        file.write_all(body)?;
        file.sync_all()?;
    }
    Ok(())
}
/// Executes only the embedded, authenticated installer with its embedded bundle.
/// Inputs select a verb and reviewed digest, never an executable or source path.
pub fn lifecycle(verb: &str, expected: &str) -> Result<()> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("operator installation requires the root operator".into());
    }
    if !matches!(verb, "plan" | "apply" | "status" | "recover") {
        return Err("unsupported operator lifecycle verb".into());
    }
    let bundle: BTreeMap<_, _> = FILES
        .iter()
        .map(|(name, body)| {
            (
                *name,
                std::str::from_utf8(body).expect("embedded Python and systemd UTF-8"),
            )
        })
        .collect();
    let wire = serde_json::to_vec(&bundle)?;
    let mut signals = signal_hook::iterator::Signals::new([
        signal_hook::consts::SIGINT,
        signal_hook::consts::SIGTERM,
    ])?;
    let mut command = Command::new("/usr/bin/python3");
    command
        .args([
            "-I",
            "-c",
            include_str!("../internal/operator/assets/install_operator.py"),
            verb,
            "--expected-plan",
            expected,
        ])
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LC_ALL", "C")
        .current_dir("/")
        .stdin(Stdio::piped())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .process_group(0);
    let mut child = command.spawn()?;
    let mut input = child.stdin.take().ok_or("installer stdin unavailable")?;
    let writer = std::thread::spawn(move || input.write_all(&wire));
    let deadline = Instant::now() + Duration::from_secs(150);
    let outcome: Result<()> = loop {
        if let Some(status) = child.try_wait()? {
            break if status.success() {
                Ok(())
            } else {
                Err(format!("operator installer exited with {status}").into())
            };
        }
        if signals.pending().next().is_some() || Instant::now() >= deadline {
            // This child and its descendants have a dedicated process group.
            unsafe {
                libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
            }
            child.wait()?;
            break Err("operator installation cancelled or timed out; inspect status and recover prepared state".into());
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let written = writer.join().map_err(|_| "installer input writer failed")?;
    outcome?;
    written?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn cold_export_matches_every_embedded_byte_and_preserves_prior_stages() {
        let root = tempfile::tempdir().unwrap();
        let stage = root.path().join("stage");
        export(&stage).unwrap();
        assert_eq!(
            stage.metadata().unwrap().permissions().mode() & 0o777,
            0o700
        );
        for (name, want) in plan() {
            let body = std::fs::read(stage.join(name)).unwrap();
            assert_eq!(
                Sha256::digest(body)
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>(),
                want
            );
            assert_eq!(
                stage.join(name).metadata().unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        std::fs::write(stage.join("reviewed"), b"keep").unwrap();
        assert!(export(&stage).is_err());
        assert_eq!(std::fs::read(stage.join("reviewed")).unwrap(), b"keep");
        for path in [
            "/",
            "relative",
            "/tmp/./operator",
            "/tmp/../operator",
            "/tmp//operator",
            "/tmp/operator/",
        ] {
            assert!(export(Path::new(path)).is_err(), "{path}");
        }
    }
}
