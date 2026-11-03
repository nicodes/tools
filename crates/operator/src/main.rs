#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("The tools-operator CLI supports Linux and macOS only.");
#[cfg(any(target_os = "linux", target_os = "macos"))]
fn run() -> Result<(), Box<dyn std::error::Error>> {
    use std::{
        ffi::OsString,
        io::{self, Read, Write},
        path::Path,
    };
    use tools_operator::{bundle, metadata::metadata};
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let Some(verb) = args.first().and_then(|s| s.to_str()) else {
        return Err(
            "usage: tools-operator plan|apply|status|recover|export|metadata|version".into(),
        );
    };
    if verb == "version" && args.len() == 1 {
        println!("{}", env!("OPERATOR_VERSION"));
        return Ok(());
    }
    if verb == "--build-info" && args.len() == 1 {
        println!(
            "{}",
            serde_json::json!({"version":env!("OPERATOR_VERSION"),"commit":env!("OPERATOR_COMMIT"),"target":env!("OPERATOR_TARGET"),"bundle":bundle::plan()})
        );
        return Ok(());
    }
    if verb == "metadata" {
        if args.len() != 1 {
            return Err("metadata accepts only one bounded YAML document on stdin".into());
        }
        let mut body = Vec::new();
        io::stdin().take(65537).read_to_end(&mut body)?;
        let value = metadata(&body)?;
        let mut output = serde_json::to_vec(&value)?;
        output.push(b'\n');
        io::stdout().write_all(&output)?;
        return Ok(());
    }
    let mut expected = OsString::new();
    let mut directory = OsString::new();
    let mut i = 1;
    while i < args.len() {
        let flag = args[i].to_str().ok_or("invalid flag encoding")?;
        let flag = flag
            .strip_prefix("--")
            .or_else(|| flag.strip_prefix('-'))
            .ok_or("unexpected positional inputs")?;
        let (name, inline) = flag.split_once('=').map_or((flag, None), |(name, value)| {
            (name, Some(OsString::from(value)))
        });
        if name != "expected-plan" && name != "directory" {
            return Err(format!("unknown flag: {name}").into());
        }
        let value = if let Some(value) = inline {
            value
        } else {
            i += 1;
            args.get(i).ok_or("flag requires a value")?.clone()
        };
        match name {
            "expected-plan" => expected = value,
            "directory" => directory = value,
            _ => unreachable!(),
        };
        i += 1;
    }
    if verb == "export" {
        if !expected.is_empty() {
            return Err("export accepts no expected plan".into());
        }
        bundle::export(Path::new(&directory))?;
        println!(
            "{}",
            serde_json::json!({"version":1,"directory":directory.to_string_lossy(),"files":bundle::plan()})
        );
        return Ok(());
    }
    if !directory.is_empty() || verb != "apply" && !expected.is_empty() {
        return Err("only export accepts a directory; only apply accepts an expected plan".into());
    }
    bundle::lifecycle(
        verb,
        expected.to_str().ok_or("expected plan must be UTF-8")?,
    )
}
#[cfg(any(target_os = "linux", target_os = "macos"))]
fn main() {
    if let Err(error) = run() {
        eprintln!("error: {error}");
        std::process::exit(1);
    }
}
