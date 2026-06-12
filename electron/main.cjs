const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const net = require("node:net");
const { autoUpdater } = require("electron-updater");

const APP_NAME = "codex switch";
const BACKEND_HOST = process.env.CODEX_SWITCH_HOST || "127.0.0.1";
const DEFAULT_BACKEND_PORT = Number.parseInt(process.env.CODEX_SWITCH_PORT || "8765", 10) || 8765;
const EXTERNAL_BACKEND_URL = process.env.CODEX_SWITCH_URL || null;
const FORCE_OWN_BACKEND =
  process.env.CODEX_SWITCH_FORCE_OWN_BACKEND === "1" || (!app.isPackaged && !EXTERNAL_BACKEND_URL);
const REUSE_EXISTING_BACKEND =
  process.env.CODEX_SWITCH_REUSE_EXISTING_BACKEND === "1" && !FORCE_OWN_BACKEND;
const PROJECT_ROOT = path.resolve(__dirname, "..");
const PRELOAD_PATH = path.join(__dirname, "preload.cjs");
let backendProcess = null;
let activeBackendUrl = EXTERNAL_BACKEND_URL;
let activeBackendPort = EXTERNAL_BACKEND_URL ? null : DEFAULT_BACKEND_PORT;
let mainWindow = null;
let updaterInitialized = false;
let updateInstallRequested = false;
let updaterState = {
  supported: false,
  configured: false,
  phase: "unavailable",
  version: null,
  progressPercent: null,
  message: "Automatic updates are available only in the installed desktop app.",
  checkedAt: null
};

const hasSingleInstanceLock =
  process.env.CODEX_SWITCH_ALLOW_MULTI_INSTANCE === "1" || FORCE_OWN_BACKEND
    ? true
    : app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
}

app.setName(APP_NAME);

function sendUpdaterState() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  mainWindow.webContents.send("updater:state", updaterState);
}

function setUpdaterState(nextState) {
  updaterState = {
    ...updaterState,
    ...nextState
  };
  sendUpdaterState();
}

function updaterConfigPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app-update.yml");
  }
  return path.join(PROJECT_ROOT, "dev-app-update.yml");
}

function hasUpdaterConfig() {
  return fs.existsSync(updaterConfigPath());
}

function initializeAutoUpdater() {
  if (updaterInitialized || !hasUpdaterConfig()) {
    if (!hasUpdaterConfig()) {
      setUpdaterState({
        supported: false,
        configured: false,
        phase: "unavailable",
        message: "No update feed is configured for this build yet."
      });
    }
    return;
  }

  updaterInitialized = true;
  autoUpdater.forceDevUpdateConfig = !app.isPackaged;
  autoUpdater.updateConfigPath = updaterConfigPath();
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;

  setUpdaterState({
    supported: true,
    configured: true,
    phase: "idle",
    message: "Check for updates to download a newer build.",
    checkedAt: null
  });

  autoUpdater.on("checking-for-update", () => {
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "checking",
      progressPercent: null,
      message: "Checking for updates..."
    });
  });

  autoUpdater.on("update-available", (info) => {
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "available",
      version: info?.version || null,
      progressPercent: null,
      message: info?.version
        ? `Version ${info.version} is available.`
        : "A newer version is available.",
      checkedAt: new Date().toISOString()
    });
  });

  autoUpdater.on("update-not-available", () => {
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "up-to-date",
      version: app.getVersion(),
      progressPercent: null,
      message: `You're up to date on ${app.getVersion()}.`,
      checkedAt: new Date().toISOString()
    });
  });

  autoUpdater.on("download-progress", (progress) => {
    const percent = typeof progress?.percent === "number" ? Math.max(0, Math.min(100, progress.percent)) : null;
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "downloading",
      progressPercent: percent,
      message: percent === null ? "Downloading update..." : `Downloading update... ${Math.round(percent)}%`
    });
  });

  autoUpdater.on("update-downloaded", (info) => {
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "downloaded",
      version: info?.version || updaterState.version,
      progressPercent: 100,
      message: info?.version
        ? `Version ${info.version} is ready. Restart to update.`
        : "Update downloaded. Restart to update."
    });
  });

  autoUpdater.on("error", (error) => {
    if (updaterState.phase === "installing") {
      updateInstallRequested = false;
    }
    setUpdaterState({
      supported: true,
      configured: true,
      phase: "error",
      progressPercent: null,
      message: error?.message || "Update check failed."
    });
  });

  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((error) => {
      setUpdaterState({
        supported: true,
        configured: true,
        phase: "error",
        message: error?.message || "Update check failed."
      });
    });
  }, 2500);
}

function getAppIconPath() {
  const iconPath = path.join(__dirname, "assets", "codex-switch-icon.png");
  return fs.existsSync(iconPath) ? iconPath : undefined;
}

function waitForBackend(url, timeoutMs = 15000) {
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    const tryConnect = () => {
      const request = http.get(`${url}/api/health`, (response) => {
        response.resume();
        if (response.statusCode === 200) {
          resolve();
          return;
        }
        if (Date.now() - startedAt > timeoutMs) {
          reject(new Error(`Backend health check returned ${response.statusCode}`));
          return;
        }
        setTimeout(tryConnect, 200);
      });

      request.on("error", () => {
        if (Date.now() - startedAt > timeoutMs) {
          reject(new Error("Timed out waiting for backend"));
          return;
        }
        setTimeout(tryConnect, 200);
      });
    };

    tryConnect();
  });
}

function checkBackendHealth(url, timeoutMs = 750) {
  return new Promise((resolve) => {
    const request = http.get(`${url}/api/health`, { timeout: timeoutMs }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });

    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });

    request.on("error", () => {
      resolve(false);
    });
  });
}

function isPortAvailable(host, port) {
  return new Promise((resolve) => {
    const server = net.createServer();

    server.once("error", (error) => {
      if (error && error.code === "EADDRINUSE") {
        resolve(false);
        return;
      }
      resolve(false);
    });

    server.once("listening", () => {
      server.close(() => resolve(true));
    });

    server.listen(port, host);
  });
}

function findAvailablePort(host) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();

    server.once("error", reject);
    server.once("listening", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => reject(new Error("Failed to allocate a backend port.")));
        return;
      }
      const { port } = address;
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(port);
      });
    });

    server.listen(0, host);
  });
}

function getStaticRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "web", "dist");
  }
  return path.join(PROJECT_ROOT, "web", "dist");
}

function getPackagedBackendPath() {
  const executableName = process.platform === "win32" ? "codex-switch-backend.exe" : "codex-switch-backend";
  const onefilePath = path.join(process.resourcesPath, "backend", executableName);
  if (fs.existsSync(onefilePath) && fs.statSync(onefilePath).isFile()) {
    return onefilePath;
  }
  return path.join(process.resourcesPath, "backend", "codex-switch-backend", executableName);
}

function getBackendLaunchConfig(port) {
  const serverArgs = ["--host", BACKEND_HOST, "--port", String(port), "--static-root", getStaticRoot()];

  if (app.isPackaged) {
    const backendPath = getPackagedBackendPath();
    if (!fs.existsSync(backendPath)) {
      throw new Error(`Packaged backend was not found at ${backendPath}`);
    }
    return {
      command: backendPath,
      args: serverArgs,
      cwd: app.getPath("userData")
    };
  }

  return {
    command: "python3",
    args: ["-m", "codex_profile_switcher.server", ...serverArgs],
    cwd: PROJECT_ROOT
  };
}

function startBackend(port) {
  if (backendProcess || EXTERNAL_BACKEND_URL) {
    return;
  }

  const backend = getBackendLaunchConfig(port);
  backendProcess = spawn(backend.command, backend.args, {
    cwd: backend.cwd,
    env: {
      ...process.env,
      CODEX_SWITCH_APP_VERSION: app.getVersion()
    },
    stdio: app.isPackaged ? "ignore" : "inherit",
    windowsHide: true
  });

  backendProcess.on("exit", () => {
    backendProcess = null;
    activeBackendUrl = EXTERNAL_BACKEND_URL;
    activeBackendPort = EXTERNAL_BACKEND_URL ? null : DEFAULT_BACKEND_PORT;
  });
}

async function resolveBackendTarget() {
  if (EXTERNAL_BACKEND_URL) {
    activeBackendUrl = EXTERNAL_BACKEND_URL;
    activeBackendPort = null;
    return { url: EXTERNAL_BACKEND_URL, port: null, shouldSpawn: false };
  }

  if (REUSE_EXISTING_BACKEND && activeBackendUrl && await checkBackendHealth(activeBackendUrl)) {
    return { url: activeBackendUrl, port: activeBackendPort, shouldSpawn: false };
  }

  const preferredUrl = `http://${BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`;
  if (REUSE_EXISTING_BACKEND && await checkBackendHealth(preferredUrl)) {
    activeBackendUrl = preferredUrl;
    activeBackendPort = DEFAULT_BACKEND_PORT;
    return { url: preferredUrl, port: DEFAULT_BACKEND_PORT, shouldSpawn: false };
  }

  if (await isPortAvailable(BACKEND_HOST, DEFAULT_BACKEND_PORT)) {
    activeBackendUrl = preferredUrl;
    activeBackendPort = DEFAULT_BACKEND_PORT;
    return { url: preferredUrl, port: DEFAULT_BACKEND_PORT, shouldSpawn: true };
  }

  const fallbackPort = await findAvailablePort(BACKEND_HOST);
  const fallbackUrl = `http://${BACKEND_HOST}:${fallbackPort}`;
  activeBackendUrl = fallbackUrl;
  activeBackendPort = fallbackPort;
  return { url: fallbackUrl, port: fallbackPort, shouldSpawn: true };
}

async function createWindow() {
  const backendTarget = await resolveBackendTarget();
  if (backendTarget.shouldSpawn && backendTarget.port !== null) {
    startBackend(backendTarget.port);
  }
  await waitForBackend(backendTarget.url);
  const icon = getAppIconPath();

  mainWindow = new BrowserWindow({
    title: APP_NAME,
    width: 1320,
    height: 860,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: "#071018",
    autoHideMenuBar: true,
    icon,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      preload: PRELOAD_PATH
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (!app.isPackaged) {
    await mainWindow.webContents.session.clearCache().catch(() => {});
    await mainWindow.webContents.session
      .clearStorageData({ storages: ["appcache", "cachestorage", "serviceworkers"] })
      .catch(() => {});
  }

  const loadUrl = app.isPackaged ? backendTarget.url : `${backendTarget.url}?dev=${Date.now()}`;
  await mainWindow.loadURL(loadUrl);
  sendUpdaterState();
  initializeAutoUpdater();
}

app.on("second-instance", () => {
  if (!mainWindow) {
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.focus();
});

ipcMain.handle("updater:get-state", async () => updaterState);
ipcMain.handle("updater:check", async () => {
  initializeAutoUpdater();
  if (!updaterInitialized) {
    return updaterState;
  }
  await autoUpdater.checkForUpdates();
  return updaterState;
});
ipcMain.handle("updater:download", async () => {
  initializeAutoUpdater();
  if (!updaterInitialized) {
    return updaterState;
  }
  await autoUpdater.downloadUpdate();
  return updaterState;
});
ipcMain.handle("updater:install", async () => {
  if (!updaterInitialized || updaterState.phase !== "downloaded") {
    return updaterState;
  }
  updateInstallRequested = true;
  setUpdaterState({
    supported: true,
    configured: true,
    phase: "installing",
    progressPercent: 100,
    message: updaterState.version
      ? `Installing version ${updaterState.version}. Codex Switch will reopen automatically.`
      : "Installing update. Codex Switch will reopen automatically."
  });

  setTimeout(() => {
    const isSilent = process.platform === "win32";
    try {
      autoUpdater.quitAndInstall(isSilent, true);
    } catch (error) {
      updateInstallRequested = false;
      setUpdaterState({
        phase: "error",
        progressPercent: null,
        message: error?.message || "Could not start the update installer."
      });
    }
  }, 900);

  return updaterState;
});
ipcMain.handle("shell:open-external", async (_event, url) => {
  if (typeof url !== "string") {
    throw new Error("Missing URL");
  }
  const parsedUrl = new URL(url);
  if (!["http:", "https:"].includes(parsedUrl.protocol)) {
    throw new Error("Only http and https links can be opened.");
  }
  await shell.openExternal(parsedUrl.toString());
  return { ok: true };
});

app.whenReady().then(async () => {
  app.setAboutPanelOptions({ applicationName: APP_NAME });
  const icon = getAppIconPath();
  if (icon && process.platform === "darwin" && app.dock) {
    app.dock.setIcon(icon);
  }
  await createWindow();
  app.on("activate", async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  if (!updateInstallRequested && backendProcess) {
    backendProcess.kill("SIGTERM");
    backendProcess = null;
  }
});

app.on("quit", () => {
  if (backendProcess) {
    backendProcess.kill("SIGTERM");
    backendProcess = null;
  }
});
