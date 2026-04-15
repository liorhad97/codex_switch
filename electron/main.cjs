const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const http = require("node:http");

const BACKEND_URL = process.env.CODEX_SWITCH_URL || "http://127.0.0.1:8765";
let backendProcess = null;

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

function startBackend() {
  if (backendProcess) {
    return;
  }
  const projectRoot = path.resolve(__dirname, "..");
  backendProcess = spawn(
    "python3",
    ["-m", "codex_profile_switcher.server", "--static-root", path.join(projectRoot, "web", "dist")],
    {
      cwd: projectRoot,
      env: process.env,
      stdio: "inherit"
    }
  );

  backendProcess.on("exit", () => {
    backendProcess = null;
  });
}

async function createWindow() {
  startBackend();
  await waitForBackend(BACKEND_URL);

  const window = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: "#071018",
    autoHideMenuBar: true,
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
