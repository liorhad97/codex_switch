const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const rootDir = path.resolve(__dirname, "..");
const bucketName = process.env.CODEX_SWITCH_R2_BUCKET || "codex-switch-updates";
const updateBaseUrl =
  process.env.CODEX_SWITCH_UPDATE_BASE_URL || "https://pub-1fc6be6e977a4adf8a928d5e615d8f54.r2.dev";

function fail(message) {
  console.error(message);
  process.exit(1);
}

function runWrangler(args) {
  const result = spawnSync("npx", ["wrangler", ...args], {
    cwd: rootDir,
    stdio: "inherit"
  });

  if (result.error) {
    fail(result.error.message);
  }

  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

function candidateReleaseDirs() {
  if (process.env.CODEX_SWITCH_RELEASE_DIR) {
    return [path.resolve(rootDir, process.env.CODEX_SWITCH_RELEASE_DIR)];
  }

  return ["release", "mac-installer", "windows-installer"].map((dirName) => path.join(rootDir, dirName));
}

function listReleaseFiles() {
  const filesByName = new Map();

  for (const dir of candidateReleaseDirs()) {
    if (!fs.existsSync(dir)) {
      continue;
    }

    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isFile()) {
        continue;
      }

      if (entry.name === ".gitignore" || entry.name === "builder-debug.yml") {
        continue;
      }

      filesByName.set(entry.name, {
        dir,
        fileName: entry.name,
        filePath: path.join(dir, entry.name)
      });
    }
  }

  return [...filesByName.values()].sort((left, right) => left.fileName.localeCompare(right.fileName));
}

function metadataFor(fileName) {
  const lower = fileName.toLowerCase();

  if (lower.endsWith(".yml")) {
    return {
      contentType: "text/yaml; charset=utf-8",
      cacheControl: "no-store, max-age=0"
    };
  }

  if (lower.endsWith(".zip")) {
    return {
      contentType: "application/zip",
      cacheControl: "public, max-age=31536000, immutable"
    };
  }

  if (lower.endsWith(".dmg")) {
    return {
      contentType: "application/x-apple-diskimage",
      cacheControl: "public, max-age=31536000, immutable"
    };
  }

  if (lower.endsWith(".exe")) {
    return {
      contentType: "application/vnd.microsoft.portable-executable",
      cacheControl: "public, max-age=31536000, immutable"
    };
  }

  if (lower.endsWith(".blockmap")) {
    return {
      contentType: "application/octet-stream",
      cacheControl: "public, max-age=31536000, immutable"
    };
  }

  return {
    cacheControl: "public, max-age=31536000, immutable"
  };
}

const releaseFiles = listReleaseFiles();

if (releaseFiles.length === 0) {
  fail("No release files found in release/, mac-installer/, or windows-installer/.");
}

for (const releaseFile of releaseFiles) {
  const metadata = metadataFor(releaseFile.fileName);
  const args = [
    "r2",
    "object",
    "put",
    `${bucketName}/${releaseFile.fileName}`,
    "--remote",
    "--file",
    releaseFile.filePath
  ];

  if (metadata.contentType) {
    args.push("--content-type", metadata.contentType);
  }

  if (metadata.cacheControl) {
    args.push("--cache-control", metadata.cacheControl);
  }

  console.log(
    `Uploading ${releaseFile.fileName} from ${releaseFile.dir} to r2://${bucketName}/${releaseFile.fileName}`
  );
  runWrangler(args);
}

console.log("");
console.log(`Uploaded ${releaseFiles.length} release artifact(s) to ${bucketName}.`);
console.log(`Updater base URL: ${updateBaseUrl}`);
