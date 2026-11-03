fn main() {
    for name in ["VERSION", "COMMIT"] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let version = std::env::var("VERSION").unwrap_or_else(|_| "dev".into());
    let commit = std::env::var("COMMIT").unwrap_or_else(|_| "unknown".into());
    assert!(!version.contains(['\n', '\r']) && !commit.contains(['\n', '\r']));
    println!("cargo:rustc-env=OPERATOR_VERSION={version}");
    println!("cargo:rustc-env=OPERATOR_COMMIT={commit}");
    println!(
        "cargo:rustc-env=OPERATOR_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
}
