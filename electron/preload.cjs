const { contextBridge, ipcRenderer, webUtils } = require("electron");

async function invoke(channel, ...args) {
    const result = await ipcRenderer.invoke(`subtitle:${channel}`, ...args);
    if (result.error !== undefined) throw new Error(result.error);
    return result.value;
}

contextBridge.exposeInMainWorld("subtitle", {
    call: (command, ...args) => invoke("call", command, args),
    pick: (kind, directory) => invoke("pick", kind, directory),
    save: (kind, filename, text) => invoke("save", kind, filename, text),
    reveal: (path) => invoke("reveal", path),
    theme: () => invoke("theme"),
    confirm: (message, detail) => invoke("confirm", message, detail),
    filePath: (file) => webUtils.getPathForFile(file),
    onEvent: (listener) => {
        ipcRenderer.on("subtitle:event", (_event, data) => listener(data));
    },
});
