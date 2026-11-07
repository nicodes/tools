#!/usr/bin/env python3
"""Prepare pinned caller-local Chromium and an optional Playwright CLI launcher."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def package_directory(root, name):
    root = Path(root).resolve()
    value = Path(name)
    if value.is_absolute() or ".." in value.parts or "\\" in name:
        raise ValueError("browser package must stay inside the checkout")
    directory = root / value
    if not directory.resolve().is_relative_to(root) or any(p.is_symlink() for p in [directory, *directory.parents] if p.is_relative_to(root)):
        raise ValueError("browser package may not use symlinks")
    if not (directory / "package-lock.json").is_file():
        raise ValueError("browser setup requires a caller-owned package lock")
    return directory


def prepare(root, package, *, cli_version=None, run=subprocess.run):
    root = Path(root).resolve()
    directory = package_directory(root, package)
    artifacts = root / ".artifacts"
    for output in [artifacts, artifacts / "browser-config.json", artifacts / "playwright-cli", artifacts / "playwright"]:
        if output.is_symlink() or not output.resolve().is_relative_to(root):
            raise ValueError("browser outputs must stay inside the checkout without symlinks")
    manifest = json.loads((directory / "package.json").read_text())
    dependencies = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    # SDK packages stay exact and lock-bound; setup never invokes npx downloads.
    browser_package = next((name for name in ("playwright", "@playwright/test") if name in dependencies), None)
    if browser_package is None or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", dependencies[browser_package]):
        raise ValueError("declare an exact Playwright SDK in the locked browser package")
    if cli_version is not None and (dependencies.get("@playwright/cli") != cli_version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", cli_version)):
        raise ValueError("declare the exact CLI SDK alongside Playwright")
    local = directory / "node_modules/.bin"
    binary = local / "playwright"
    if not binary.is_file():
        raise ValueError("install the locked browser package with mise run setup first")
    environment = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(root / ".artifacts/playwright")}
    command = [str(binary), "install", "chromium"]
    if os.environ.get("CI") == "true":
        command.append("--with-deps")
    run(command, cwd=root, env=environment, check=True)
    if cli_version is None:
        return
    if dependencies.get("@playwright/cli") != cli_version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", cli_version):
        raise ValueError("declare the exact CLI SDK alongside Playwright")
    cli = local / "playwright-cli"
    result = run([str(cli), "--version"], cwd=root, env=environment, check=True, capture_output=True, text=True)
    if result.stdout.strip() != cli_version:
        raise ValueError("installed Playwright CLI version differs from the caller pin")
    module = str(directory / "node_modules/playwright")
    expression = "const {chromium}=require(process.argv[1]); console.log(chromium.executablePath())"
    executable = run(["node", "-e", expression, module], cwd=root, env=environment, check=True, capture_output=True, text=True).stdout.strip()
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise ValueError("pinned Chromium executable is missing")
    config = root / ".artifacts/browser-config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"browser": {"browserName": "chromium", "launchOptions": {"executablePath": executable, "headless": True, "chromiumSandbox": os.getuid() != 0 if hasattr(os, "getuid") else True}}}) + "\n")
    launcher = root / ".artifacts/playwright-cli"
    argv = [sys.executable, str(Path(__file__).resolve()), "cli", "--root", str(root), "--package", package, "--"]
    launcher.write_text("#!/bin/sh\nexec " + " ".join(map(shlex.quote, argv)) + ' "$@"\n')
    launcher.chmod(0o700)


def cli(root, package, arguments):
    root = Path(root).resolve()
    directory = package_directory(root, package)
    command = [str(directory / "node_modules/.bin/playwright-cli"), *arguments]
    if "open" in arguments:
        config = root / ".artifacts/browser-config.json"
        if not config.is_file() or config.is_symlink():
            raise ValueError("prepare the pinned browser config with mise run setup first")
        command += ["--config", str(config)]
    os.execvpe(command[0], command, {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(root / ".artifacts/playwright")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["setup", "cli"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--package", required=True)
    parser.add_argument("--cli-version")
    options, arguments = parser.parse_known_args()
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        if options.operation == "setup":
            if arguments:
                raise ValueError("setup does not accept browser CLI arguments")
            prepare(options.root, options.package, cli_version=options.cli_version)
        else:
            cli(options.root, options.package, arguments)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
