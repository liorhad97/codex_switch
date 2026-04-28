const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("codexSwitchUpdater", {
  getState: () => ipcRenderer.invoke("updater:get-state"),
  checkForUpdates: () => ipcRenderer.invoke("updater:check"),
  downloadUpdate: () => ipcRenderer.invoke("updater:download"),
  installUpdate: () => ipcRenderer.invoke("updater:install"),
  onStateChanged: (callback) => {
    if (typeof callback !== "function") {
      return () => {};
    }

    const listener = (_event, payload) => {
      callback(payload);
    };

    ipcRenderer.on("updater:state", listener);
    return () => {
      ipcRenderer.removeListener("updater:state", listener);
    };
  }
});

contextBridge.exposeInMainWorld("codexSwitchShell", {
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url)
});
