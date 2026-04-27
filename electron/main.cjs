const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");

const APP_NAME = "codex switch";
const BACKEND_HOST = process.env.CODEX_SWITCH_HOST || "127.0.0.1";
const BACKEND_PORT = process.env.CODEX_SWITCH_PORT || "8765";
const BACKEND_URL = process.env.CODEX_SWITCH_URL || `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const PROJECT_ROOT = path.resolve(__dirname, "..");
let backendProcess = null;

app.setName(APP_NAME);

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

function getStaticRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "web", "dist");
  }
  return path.join(PROJECT_ROOT, "web", "dist");
}

function getPackagedBackendPath() {
  const executableName = process.platform === "win32" ? "codex-switch-backend.exe" : "codex-switch-backend";
  return path.join(process.resourcesPath, "backend", executableName);
}

function getBackendLaunchConfig() {
  const serverArgs = ["--host", BACKEND_HOST, "--port", BACKEND_PORT, "--static-root", getStaticRoot()];

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

function startBackend() {
  if (backendProcess || process.env.CODEX_SWITCH_URL) {
    return;
  }

  const backend = getBackendLaunchConfig();
  backendProcess = spawn(backend.command, backend.args, {
    cwd: backend.cwd,
    env: process.env,
    stdio: app.isPackaged ? "ignore" : "inherit",
    windowsHide: true
  });

  backendProcess.on("exit", () => {
    backendProcess = null;
  });
}

async function createWindow() {
  startBackend();
  await waitForBackend(BACKEND_URL);
  const icon = getAppIconPath();

  const window = new BrowserWindow({
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
      sandbox: true
    }
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  await window.loadURL(BACKEND_URL);
}

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
  if (backendProcess) {
    backendProcess.kill("SIGTERM");
    backendProcess = null;
  }
});
