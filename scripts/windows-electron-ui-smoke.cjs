const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

const repoRoot = path.resolve(__dirname, "..");
const installerRoot = path.join(repoRoot, "windows-installer");
const appName = "codex switch.exe";
const activeProcessIds = new Set();

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function killProcessTree(processId) {
  if (!processId) {
    return;
  }
  try {
    execFileSync("taskkill", ["/PID", String(processId), "/T", "/F"], { stdio: "ignore" });
  } catch {
    // The process may have already exited.
  }
}

function cleanupFakeCodexProcesses() {
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

function cleanupRunningApps() {
  for (const processId of activeProcessIds) {
    killProcessTree(processId);
  }
  activeProcessIds.clear();
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

function createWindowsAppEnv(testRoot, backendPort) {
  const userProfile = path.join(testRoot, "User");
  const localAppData = path.join(userProfile, "AppData", "Local");
  const roamingAppData = path.join(userProfile, "AppData", "Roaming");
  fs.mkdirSync(localAppData, { recursive: true });
  fs.mkdirSync(roamingAppData, { recursive: true });

  const env = {
    ...process.env,
    USERPROFILE: userProfile,
    HOME: userProfile,
    LOCALAPPDATA: localAppData,
    APPDATA: roamingAppData,
    CODEX_BINARY: writeFakeCodex(testRoot),
    CODEX_SWITCH_PORT: String(backendPort),
    CODEX_SWITCH_FORCE_OWN_BACKEND: "1",
    CODEX_SWITCH_ALLOW_MULTI_INSTANCE: "1",
    ELECTRON_ENABLE_LOGGING: "1",
    NO_PROXY: "127.0.0.1,localhost"
  };
  delete env.CODEX_SWITCH_URL;

  return { env, userProfile };
}

async function waitForHttpOk(url, timeoutMs, description) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return response;
      }
      lastError = new Error(`${url} returned HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }

  throw new Error(`Timed out waiting for ${description}. ${lastError?.message || ""}`.trim());
}

async function waitForAppPage(browser, backendPort) {
  const expectedUrlPrefix = `http://127.0.0.1:${backendPort}`;
  const deadline = Date.now() + 45000;

  while (Date.now() < deadline) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page = pages.find((candidate) => candidate.url().startsWith(expectedUrlPrefix));
    if (page) {
      return page;
    }
    await delay(250);
  }

  const urls = browser
    .contexts()
    .flatMap((context) => context.pages())
    .map((page) => page.url())
    .join(", ");
  throw new Error(`Timed out waiting for Electron renderer page at ${expectedUrlPrefix}. Open pages: ${urls}`);
}

async function launchPackagedApp({ appExe, testRoot, backendPort, debugPort }) {
  const { env, userProfile } = createWindowsAppEnv(testRoot, backendPort);
  const args = [
    `--remote-debugging-port=${debugPort}`,
    "--disable-gpu",
    "--disable-software-rasterizer"
  ];
  const appProcess = spawn(appExe, args, {
    cwd: path.dirname(appExe),
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true
  });
  activeProcessIds.add(appProcess.pid);
  appProcess.on("exit", () => {
    activeProcessIds.delete(appProcess.pid);
  });

  appProcess.stdout.on("data", (chunk) => {
    const text = chunk.toString().trim();
    if (text) {
      console.log(`[electron:stdout] ${text}`);
    }
  });
  appProcess.stderr.on("data", (chunk) => {
    const text = chunk.toString().trim();
    if (text) {
      console.log(`[electron:stderr] ${text}`);
    }
  });

  let browser = null;
  try {
    await waitForHttpOk(`http://127.0.0.1:${debugPort}/json/version`, 45000, "Electron remote debugging");
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
    const page = await waitForAppPage(browser, backendPort);
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

    return {
      page,
      userProfile,
      async close() {
        if (browser) {
          await browser.close().catch(() => {});
          browser = null;
        }
        killProcessTree(appProcess.pid);
        activeProcessIds.delete(appProcess.pid);
        cleanupFakeCodexProcesses();
      }
    };
  } catch (error) {
    if (browser) {
      await browser.close().catch(() => {});
    }
    killProcessTree(appProcess.pid);
    activeProcessIds.delete(appProcess.pid);
    cleanupFakeCodexProcesses();
    throw error;
  }
}

async function verifyAddAccountPopup(appExe, baseRoot) {
  console.log("Starting Windows zip UI check: Add Account pop-up.");
  const testRoot = path.join(baseRoot, "add-account");
  fs.rmSync(testRoot, { recursive: true, force: true });
  fs.mkdirSync(testRoot, { recursive: true });

  const app = await launchPackagedApp({
    appExe,
    testRoot,
    backendPort: 18865,
    debugPort: 18965
  });

  try {
    await app.page.getByRole("button", { name: "Add Account" }).click();
    await app.page.getByRole("dialog", { name: "Add account sign-in" }).waitFor();
    await app.page.getByText("Code: WIN-UI-123").waitFor();
    const signInLink = await app.page.getByLabel("Sign-in link").inputValue();
    assert(
      signInLink === "https://chat.openai.com/auth/windows-ui-smoke",
      `Unexpected sign-in link: ${signInLink}`
    );
    console.log("Add Account opened the Windows sign-in pop-up with the expected code and link.");
  } finally {
    await app.close();
  }
}

async function verifyFixCommonIssuesButton(appExe, baseRoot) {
  console.log("Starting Windows zip UI check: Fix Common Switch Issues.");
  const testRoot = path.join(baseRoot, "fix-common-issues");
  fs.rmSync(testRoot, { recursive: true, force: true });
  fs.mkdirSync(testRoot, { recursive: true });

  const app = await launchPackagedApp({
    appExe,
    testRoot,
    backendPort: 18866,
    debugPort: 18966
  });

  try {
    const skeletonRoot = createSkeletonProfile(app.userProfile);
    assert(fs.existsSync(skeletonRoot), `Failed to create fake skeleton profile at ${skeletonRoot}`);
    console.log(`Created fake Windows skeleton profile: ${skeletonRoot}`);

    await app.page.getByLabel("Open settings").click();
    await app.page.getByRole("dialog", { name: "App Controls" }).waitFor();
    await app.page.getByRole("button", { name: "Fix Common Switch Issues" }).click();
    await app.page
      .getByRole("status")
      .filter({ hasText: /Common issue scan finished\. [1-9]\d* items? fixed\./ })
      .waitFor();
    assert(!fs.existsSync(skeletonRoot), `Fix Common Switch Issues did not remove ${skeletonRoot}`);
    console.log("Fix Common Switch Issues removed the fake Windows skeleton account.");
  } finally {
    await app.close();
  }
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
    (_fullPath, fileName) => fileName.toLowerCase() === appName
  );
  assert(appExe, `Could not find ${appName} after unzipping ${zipPath}`);

  await verifyAddAccountPopup(appExe, testRoot);
  await verifyFixCommonIssuesButton(appExe, testRoot);
  console.log(`Windows Electron UI smoke test passed using zip: ${zipPath}`);
}

const timeout = setTimeout(() => {
  console.error("Windows Electron UI smoke timed out.");
  cleanupRunningApps();
  cleanupFakeCodexProcesses();
  process.exit(1);
}, 180000);

main()
  .then(() => {
    clearTimeout(timeout);
  })
  .catch((error) => {
    clearTimeout(timeout);
    console.error(error);
    process.exit(1);
  });
