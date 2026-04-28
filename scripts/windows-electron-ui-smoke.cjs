const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { _electron: electron } = require("playwright");

const repoRoot = path.resolve(__dirname, "..");
const installerRoot = path.join(repoRoot, "windows-installer");

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function findFirstFile(root, predicate) {
  const pending = [root];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(fullPath);
        continue;
      }
      if (entry.isFile() && predicate(fullPath, entry.name)) {
        return fullPath;
      }
    }
  }
  return null;
}

function powershellQuote(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function expandZip(zipPath, destination) {
  execFileSync(
    "powershell",
    [
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      `Expand-Archive -LiteralPath ${powershellQuote(zipPath)} -DestinationPath ${powershellQuote(destination)} -Force`
    ],
    { stdio: "inherit" }
  );
}

function writeFakeCodex(testRoot) {
  const fakeCodexScript = path.join(testRoot, "fake-codex-app-server.cjs");
  fs.writeFileSync(
    fakeCodexScript,
    `const readline = require("node:readline");

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

function respond(id, result = {}) {
  process.stdout.write(JSON.stringify({ id, result }) + "\\n");
}

rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (!message.id) {
    return;
  }

  if (message.method === "account/login/start") {
    respond(message.id, {
      authUrl: "https://chat.openai.com/auth/windows-ui-smoke",
      userCode: "WIN-UI-123"
    });
    return;
  }

  if (message.method === "account/read") {
    respond(message.id, {
      account: {
        email: "windows-ui-smoke@example.com",
        planType: "plus",
        type: "chatgpt"
      }
    });
    return;
  }

  if (message.method === "account/rateLimits/read") {
    respond(message.id, {
      rateLimits: {
        primary: {
          usedPercent: 12,
          resetsAt: 1800000000,
          windowDurationMins: 300
        }
      }
    });
    return;
  }

  respond(message.id, {});
});

setInterval(() => {}, 1000);
`,
    "utf8"
  );

  const fakeCodexCmd = path.join(testRoot, "codex.cmd");
  fs.writeFileSync(
    fakeCodexCmd,
    `@echo off\r\nnode "%~dp0fake-codex-app-server.cjs" %*\r\n`,
    "ascii"
  );
  return fakeCodexCmd;
}

function createSkeletonProfile(userProfile) {
  const skeletonRoot = path.join(
    userProfile,
    "llm_accounts_profiles",
    "codex",
    "profiles",
    "skeleton-ui-1"
  );
  const codexHome = path.join(skeletonRoot, "home", ".codex");
  fs.mkdirSync(codexHome, { recursive: true });
  const configPath = path.join(codexHome, "config.toml");
  fs.writeFileSync(configPath, "readonly skeleton", "utf8");
  fs.chmodSync(configPath, 0o444);
  return skeletonRoot;
}

async function main() {
  if (process.platform !== "win32") {
    throw new Error("windows-electron-ui-smoke.cjs must run on Windows.");
  }

  const zipPath = findFirstFile(
    installerRoot,
    (_fullPath, fileName) => /^codex switch-.+-win-x64\.zip$/.test(fileName)
  );
  assert(zipPath, `Could not find Windows zip under ${installerRoot}`);

  const tempParent = process.env.RUNNER_TEMP || os.tmpdir();
  const testRoot = path.join(tempParent, "codex-switch-windows-electron-ui-smoke");
  fs.rmSync(testRoot, { recursive: true, force: true });
  fs.mkdirSync(testRoot, { recursive: true });

  const unzipRoot = path.join(testRoot, "unzipped");
  fs.mkdirSync(unzipRoot, { recursive: true });
  expandZip(zipPath, unzipRoot);

  const appExe = findFirstFile(
    unzipRoot,
    (_fullPath, fileName) => fileName.toLowerCase() === "codex switch.exe"
  );
  assert(appExe, `Could not find codex switch.exe after unzipping ${zipPath}`);

  const userProfile = path.join(testRoot, "User");
  const localAppData = path.join(userProfile, "AppData", "Local");
  const roamingAppData = path.join(userProfile, "AppData", "Roaming");
  fs.mkdirSync(localAppData, { recursive: true });
  fs.mkdirSync(roamingAppData, { recursive: true });
  const fakeCodexCmd = writeFakeCodex(testRoot);

  const port = "18865";
  const env = {
    ...process.env,
    USERPROFILE: userProfile,
    HOME: userProfile,
    LOCALAPPDATA: localAppData,
    APPDATA: roamingAppData,
    CODEX_BINARY: fakeCodexCmd,
    CODEX_SWITCH_PORT: port,
    CODEX_SWITCH_FORCE_OWN_BACKEND: "1",
    CODEX_SWITCH_ALLOW_MULTI_INSTANCE: "1",
    ELECTRON_ENABLE_LOGGING: "1",
    NO_PROXY: "127.0.0.1,localhost"
  };
  delete env.CODEX_SWITCH_URL;

  let app;
  try {
    app = await electron.launch({
      executablePath: appExe,
      env,
      timeout: 45000
    });

    const page = await app.firstWindow({ timeout: 45000 });
    page.setDefaultTimeout(30000);
    page.on("console", (message) => {
      const text = message.text();
      if (message.type() === "error" || /error|failed/i.test(text)) {
        console.log(`[renderer:${message.type()}] ${text}`);
      }
    });
    page.on("pageerror", (error) => {
      console.log(`[renderer:pageerror] ${error.stack || error.message}`);
    });

    await page.waitForLoadState("domcontentloaded");
    await page.getByRole("button", { name: "Add Account" }).waitFor();

    await page.getByRole("button", { name: "Add Account" }).click();
    await page.getByRole("dialog", { name: "Add account sign-in" }).waitFor();
    await page.getByText("Code: WIN-UI-123").waitFor();
    const signInLink = await page.getByLabel("Sign-in link").inputValue();
    assert(
      signInLink === "https://chat.openai.com/auth/windows-ui-smoke",
      `Unexpected sign-in link: ${signInLink}`
    );
    console.log("Add Account opened the Windows sign-in pop-up.");

    await page.getByLabel("Cancel sign-in").click();
    await page.getByRole("dialog", { name: "Add account sign-in" }).waitFor({ state: "detached" });

    const skeletonRoot = createSkeletonProfile(userProfile);
    assert(fs.existsSync(skeletonRoot), `Failed to create fake skeleton profile at ${skeletonRoot}`);

    await page.getByLabel("Open settings").click();
    await page.getByRole("dialog", { name: "App Controls" }).waitFor();
    await page.getByRole("button", { name: "Fix Common Switch Issues" }).click();
    await page.getByText(/Common issue scan finished\. [1-9]\d* items? fixed\./).waitFor();
    assert(!fs.existsSync(skeletonRoot), `Fix Common Switch Issues did not remove ${skeletonRoot}`);
    console.log("Fix Common Switch Issues removed the fake Windows skeleton account.");
    console.log(`Windows Electron UI smoke test passed using zip: ${zipPath}`);
  } finally {
    if (app) {
      await app.close().catch(() => {});
    }
    try {
      execFileSync(
        "powershell",
        [
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy",
          "Bypass",
          "-Command",
          'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains("fake-codex-app-server.cjs") } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }'
        ],
        { stdio: "ignore" }
      );
    } catch {
      // Nothing to clean up.
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
