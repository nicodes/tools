// Generic addon publication guards; all product names, notes and SDK policy are caller data.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve, relative, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";

function owned(root, name) {
  if (typeof name !== "string" || !name || name.includes("\\") || name.includes("\0")) throw new Error("invalid provenance source path");
  const path = resolve(root, name);
  const rel = relative(resolve(root), path);
  if (isAbsolute(name) || rel === ".." || rel.startsWith("../")) throw new Error("provenance paths must stay inside the checkout");
  return path;
}

export function loadPolicy(root) {
  const policy = JSON.parse(readFileSync(join(root, "package-provenance.json"), "utf8"));
  for (const key of ["name", "candidate_schema", "description", "archive", "registry_archive", "vendor", "allowlist", "browser_entry", "vendor_package", "vendor_digest_field", "vendor_version_field"])
    if (typeof policy[key] !== "string" || !policy[key]) throw new Error("missing native package provenance: " + key);
  const allowlist = JSON.parse(readFileSync(owned(root, policy.allowlist), "utf8"));
  if (!Number.isSafeInteger(allowlist.zip_entries) || allowlist.zip_entries < 2) throw new Error("invalid closed package entry count");
  const parts = policy.forbidden_token_parts || [];
  if (!Array.isArray(parts) || parts.some((token) => !Array.isArray(token) || !token.length || token.some((part) => typeof part !== "string" || !part)))
    throw new Error("invalid candidate boundary token policy");
  return { ...policy, zip_entries: allowlist.zip_entries,
    forbidden_tokens: [...(policy.forbidden_tokens || []), ...parts.map((token) => token.join(""))] };
}

export function loadProvenance(root, policy = loadPolicy(root)) {
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const vendor = JSON.parse(readFileSync(owned(root, policy.vendor), "utf8"));
  return { version: pkg.version, vendorVersion: vendor.version, integrity: vendor.integrity, entries: policy.zip_entries };
}

export function assertMainRef({ ref, head, originMain }) {
  if (ref !== "refs/heads/main") throw new Error("release only runs from main");
  if (!/^[0-9a-f]{40}$/.test(head || "") || head !== originMain) throw new Error("release commit is not origin/main");
}

export function assertChecksum(actual, expected) {
  if (!/^[0-9a-f]{64}$/.test(expected || "") || !/^[0-9a-f]{64}$/.test(actual || "") || actual !== expected)
    throw new Error("tested ZIP checksum does not match");
}

export function assertReleaseIdentity({ tag, commit, version }) {
  if (!/^v\d+\.\d+\.\d+$/.test(tag || "")) throw new Error("tag is not a version tag");
  if (tag !== `v${version}`) throw new Error(`tag ${tag} does not match package version ${version}`);
  if (!/^[0-9a-f]{40}$/.test(commit || "")) throw new Error("commit is not a full SHA");
}

export function zipEntrySha256(zipPath, entry) {
  if (!zipPath || !entry || entry.split("/").includes("..") || entry.startsWith("/") || entry.includes("\\")) throw new Error("zip entry is invalid");
  return createHash("sha256").update(execFileSync("unzip", ["-p", zipPath, entry], { maxBuffer: 64 * 1024 * 1024 })).digest("hex");
}

export function buildCandidate(info, policy) {
  for (const key of ["zipSha256", "vendorBrowserSha256"])
    if (!/^[0-9a-f]{64}$/.test(info[key] || "")) throw new Error(key + " sha256 is invalid");
  if (!/^[0-9a-f]{40}$/.test(info.commit || "")) throw new Error("commit is not a full SHA");
  if (info.entries !== policy.zip_entries) throw new Error("candidate ZIP entry count differs from its closed policy");
  const doc = { schema: policy.candidate_schema, release: false, isolated_test_only: true,
    commit: info.commit, package_sha256: info.zipSha256, [policy.vendor_digest_field]: info.vendorBrowserSha256,
    plugin_version: info.version, zip_entries: info.entries, [policy.vendor_version_field]: info.vendorVersion };
  const text = JSON.stringify(doc);
  if ((policy.forbidden_tokens || []).some((value) => text.toLowerCase().includes(value.toLowerCase())) || /sk_|pk_live_|pk_test_/.test(text))
    throw new Error("candidate provenance must stay generic");
  return doc;
}

export function renderNotes(info, policy) {
  return [`# ${policy.name} ${info.tag}`, "", policy.description, "", `- Plugin version: ${info.version}`,
    `- Package: ${basename(policy.archive)}`, `- SHA256: ${info.zipSha256}`, `- ZIP entries: ${info.entries}`,
    `- ${policy.vendor_package}: ${info.vendorVersion}`, `- Integrity: ${info.integrity}`,
    `- Commit: ${info.commit}`, `- Tag: ${info.tag}`, ""].join("\n");
}

export function assertNotes(text, info) {
  for (const field of [info.tag, info.commit, info.zipSha256, info.vendorVersion, info.integrity, String(info.entries), info.version])
    if (!field || !text.includes(field)) throw new Error("release notes missing package provenance");
  if (text.includes("ACCEPTED") || text.includes("# Release failure recovery")) throw new Error("release notes must be feature and package provenance");
}

export function stripAnsi(text) { return text.replace(/\u001b\[[0-9;]*m/g, ""); }
export function godotLogProblems(text) {
  return stripAnsi(text).split(/\r?\n/).filter((line) => /^ERROR:/.test(line) || line.includes("SCRIPT ERROR") || line.includes("Parse Error") || line.includes("ObjectDB instances were leaked") || /RID allocations of type/.test(line));
}
export function assertGodotLog(text, { requireOk = false } = {}) {
  const problems = godotLogProblems(text);
  if (problems.length) throw new Error(problems.join("\n"));
  if (requireOk && !/^ok /m.test(stripAnsi(text))) throw new Error("godot log did not report an ok result");
}

export function zipDosStamp(date) {
  return { time: (date.getUTCHours() << 11) | (date.getUTCMinutes() << 5) | Math.floor(date.getUTCSeconds() / 2),
    day: ((date.getUTCFullYear() - 1980) << 9) | ((date.getUTCMonth() + 1) << 5) | date.getUTCDate() };
}
export const FIXED_ZIP_STAMP = zipDosStamp(new Date(Date.UTC(1980, 0, 1, 0, 0, 0)));

function argument(args, name) { const index = args.indexOf(name); return index < 0 ? "" : args[index + 1]; }
function digest(path) { return createHash("sha256").update(readFileSync(path)).digest("hex"); }

function main() {
  const [operation, ...args] = process.argv.slice(2);
  const root = process.cwd();
  if (operation === "log") {
    assertGodotLog(readFileSync(args.find((arg) => !arg.startsWith("--")), "utf8"), { requireOk: args.includes("--require-ok") });
    return;
  }
  if (operation === "guard") {
    if (args.includes("--main")) assertMainRef({ ref: process.env.RELEASE_REF, head: process.env.RELEASE_HEAD, originMain: process.env.RELEASE_ORIGIN_MAIN });
    else if (args.includes("--checksum")) {
      const file = argument(args, "--checksum");
      assertChecksum(digest(file.replace(/\.sha256$/, "")), readFileSync(file, "utf8").trim().split(/\s+/)[0]);
    } else throw new Error("release guard was not checked");
    return;
  }
  const policy = loadPolicy(root);
  const native = loadProvenance(root, policy);
  if (operation === "identity") {
    assertReleaseIdentity({ tag: process.env.RELEASE_TAG, commit: process.env.RELEASE_COMMIT, version: native.version });
  } else if (operation === "candidate") {
    const zip = argument(args, "--zip");
    const doc = buildCandidate({ ...native, commit: argument(args, "--commit"), zipSha256: digest(zip), vendorBrowserSha256: zipEntrySha256(zip, policy.browser_entry) }, policy);
    writeFileSync(argument(args, "--out"), JSON.stringify(doc, null, 2) + "\n");
    console.log(doc.package_sha256);
  } else if (operation === "notes") {
    const info = { ...native, tag: process.env.RELEASE_TAG || argument(args, "--tag"), commit: process.env.RELEASE_COMMIT || argument(args, "--commit"), zipSha256: process.env.PACKAGE_SHA256 || argument(args, "--zip-sha256") };
    const text = renderNotes(info, policy);
    assertNotes(text, info);
    if (args[0] === "--check") assertNotes(readFileSync(args[1], "utf8"), info);
    else writeFileSync(argument(args, "--out"), text);
  } else throw new Error("unknown addon provenance operation");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; }
}
