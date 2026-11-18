const { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, Notification, screen, shell, Tray } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { createInterface } = require("node:readline");
const { pathToFileURL } = require("node:url");

const root = path.resolve(__dirname, "..");
const icon = path.join(root, "subtitle_app", "icon.ico");
const page = path.join(__dirname, "index.html");
const commands = new Set([
    "initial_state", "save_settings", "add", "change_queue", "history", "remove_history", "cache",
    "preview", "analyze", "save_preview", "start_processing", "finish_pause", "stop",
    "model_states", "operate_model", "match", "start_media",
]);
const pending = new Map();
let requestId = 0;
let backend;
let backendClosed;
let backendError = "";
let window;
let tray;
let server;
let initial;
let busy = null;
let quitting = false;
let allowQuit = false;

function startBackend() {
    const venv = path.join(root, ".venv", "Scripts", "python.exe");
    const env = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" };
    const python = fs.existsSync(venv) ? venv : "python.exe";
    backend = spawn(python, ["-u", "-m", "subtitle_app.electron_backend"], {
        cwd: root, env, windowsHide: true,
    });
    backendClosed = new Promise((resolve) => {
        backend.once("error", (error) => { backendError = error.message; });
        backend.once("close", (code) => {
            const detail = backendError.trim() || `退出代码 ${code}`;
            for (const request of pending.values()) request.reject(new Error(detail));
            pending.clear();
            backend = null;
            busy = null;
            resolve();
            if (!quitting && window && !window.isDestroyed()) {
                window.webContents.send("subtitle:event", { type: "backend_closed", message: detail });
            }
        });
    });
    backend.stdin.on("error", (error) => { backendError = error.message; });
    backend.stderr.setEncoding("utf8");
    backend.stderr.on("data", (chunk) => { backendError = (backendError + chunk).slice(-6000); });
    createInterface({ input: backend.stdout }).on("line", (line) => {
        let message;
        try {
            message = JSON.parse(line);
        } catch {
            return;
        }
        if (message.event) {
            const event = message.event;
            if (event.type === "busy") busy = event.kind;
            if (event.type === "done" || event.type === "pause_before_embed") {
                const paused = event.type === "pause_before_embed";
                if ((!paused || !window?.isVisible()) && Notification.isSupported()) {
                    new Notification({
                        title: paused ? "字幕工具等待确认" : "本地字幕生成工具",
                        body: paused ? "打开窗口确认是否嵌入字幕" : event.message,
                        icon,
                    }).show();
                }
            }
            if (window && !window.isDestroyed()) window.webContents.send("subtitle:event", event);
        } else if (pending.has(message.id)) {
            const { resolve, reject } = pending.get(message.id);
            pending.delete(message.id);
            if (message.error !== undefined) reject(new Error(message.error));
            else resolve(message.result);
        }
    });
}

function call(command, ...args) {
    if (quitting || !backend?.stdin.writable) return Promise.reject(new Error("字幕处理后端已退出"));
    const id = ++requestId;
    return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        backend.stdin.write(JSON.stringify({ id, command, args }) + "\n", (error) => {
            if (error) {
                pending.delete(id);
                reject(error);
            }
        });
    });
}

function showWindow() {
    if (!window || window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
}

function startHandoff(port) {
    server = net.createServer((socket) => {
        let data = Buffer.alloc(0);
        const reply = (result) => socket.end(JSON.stringify(result) + "\n");
        socket.on("error", () => {});
        socket.setTimeout(5000, () => socket.destroy());
        const receive = async (chunk) => {
            data = Buffer.concat([data, chunk]);
            const end = data.indexOf(10);
            if (data.length > 64 * 1024) {
                socket.removeListener("data", receive);
                reply({ ok: false, error: "请求过大" });
                return;
            }
            if (end < 0) return;
            socket.removeListener("data", receive);
            try {
                const line = new TextDecoder("utf-8", { fatal: true }).decode(data.subarray(0, end));
                const result = await call("handoff", line);
                if (result.ok) showWindow();
                reply(result);
            } catch (error) {
                reply({ ok: false, error: error.message });
            }
        };
        socket.on("data", receive);
    });
    server.on("error", (error) => {
        window?.webContents.send("subtitle:event", {
            type: "log", level: "WARNING", message: `下载器导入端口不可用：${error.message}`,
        });
    });
    server.listen(port, "127.0.0.1");
}

function handle(channel, handler) {
    ipcMain.handle(`subtitle:${channel}`, async (event, ...args) => {
        if (event.sender !== window?.webContents || event.senderFrame?.url !== pathToFileURL(page).href) {
            throw new Error("请求来源无效");
        }
        try {
            return { value: await handler(...args) };
        } catch (error) {
            return { error: error.message };
        }
    });
}

function registerHandlers() {
    handle("call", (command, args) => {
        if (!commands.has(command)) throw new Error("未知操作");
        return call(command, ...args);
    });
    handle("confirm", async (message, detail) => {
        const { response } = await dialog.showMessageBox(window, {
            type: "question", message, detail, buttons: ["取消", "确定"],
            defaultId: 0, cancelId: 0, noLink: true,
        });
        return response === 1;
    });
    handle("pick", async (kind, defaultPath) => {
        const options = {
            folder: { title: "选择目录", properties: ["openDirectory"] },
            media: { title: "添加视频或音频", properties: ["openFile", "multiSelections"],
                filters: [{ name: "视频和音频", extensions: initial.media }] },
            videos: { title: "选择视频", properties: ["openFile", "multiSelections"],
                filters: [{ name: "视频", extensions: initial.videos }] },
            video: { title: "选择视频", properties: ["openFile"],
                filters: [{ name: "视频", extensions: initial.videos }] },
            subtitle: { title: "打开字幕", properties: ["openFile"],
                filters: [{ name: "SRT 字幕", extensions: ["srt"] }] },
        };
        if (!Object.hasOwn(options, kind)) throw new Error("无效的选择类型");
        return (await dialog.showOpenDialog(window, { defaultPath, ...options[kind] })).filePaths;
    });
    handle("save", async (kind, defaultPath, text) => {
        const subtitle = kind === "subtitle";
        if (!subtitle && kind !== "log") throw new Error("无效的保存类型");
        const result = await dialog.showSaveDialog(window, {
            title: subtitle ? "保存字幕" : "导出日志", defaultPath,
            filters: [subtitle ? { name: "SRT 字幕", extensions: ["srt"] }
                : { name: "文本文件", extensions: ["txt"] }],
        });
        if (!result.filePath) return null;
        if (subtitle) await call("save_preview", result.filePath, text);
        else await fs.promises.writeFile(result.filePath, text, "utf8");
        return result.filePath;
    });
    handle("reveal", async (file) => {
        if (!path.isAbsolute(file)) throw new Error("文件路径无效");
        try {
            await fs.promises.access(file);
            shell.showItemInFolder(file);
        } catch {
            const error = await shell.openPath(path.dirname(file));
            if (error) throw new Error(error);
        }
    });
}

function quitFromTray() {
    if (busy && dialog.showMessageBoxSync(window, {
        type: "question", message: "停止当前任务并彻底退出？",
        buttons: ["取消", "退出"], defaultId: 0, cancelId: 0, noLink: true,
    }) !== 1) return;
    app.quit();
}

const windowState = () => path.join(app.getPath("userData"), "window-state.json");

function readWindowState() {
    try {
        return JSON.parse(fs.readFileSync(windowState(), "utf8"));
    } catch {
        return {};
    }
}

function saveWindowState() {
    if (!window || window.isDestroyed()) return;
    try {
        fs.writeFileSync(windowState(), JSON.stringify({
            ...window.getNormalBounds(), maximized: window.isMaximized(), theme: nativeTheme.themeSource,
        }), "utf8");
    } catch (error) {
        console.warn(error.message);
    }
}

app.setAppUserModelId("SubtitleTool.Desktop");
if (!app.requestSingleInstanceLock()) {
    allowQuit = true;
    app.quit();
} else {
    app.on("second-instance", showWindow);
    app.whenReady().then(async () => {
        startBackend();
        initial = await call("initial_state");
        registerHandlers();
        const saved = readWindowState();
        nativeTheme.themeSource = ["dark", "light"].includes(saved.theme) ? saved.theme : "system";
        handle("theme", () => {
            nativeTheme.themeSource = nativeTheme.shouldUseDarkColors ? "light" : "dark";
            saveWindowState();
        });
        const size = initial.window;
        const position = Number.isInteger(saved.x) && Number.isInteger(saved.y)
            && screen.getAllDisplays().some(({ workArea }) => saved.x >= workArea.x
                && saved.x + 120 < workArea.x + workArea.width && saved.y >= workArea.y
                && saved.y + 32 < workArea.y + workArea.height);
        window = new BrowserWindow({
            ...(position ? { x: saved.x, y: saved.y } : {}),
            width: saved.width || size.width, height: saved.height || size.height,
            minWidth: size.min_width, minHeight: size.min_height, show: false,
            title: "本地字幕生成工具", icon, autoHideMenuBar: true,
            webPreferences: { preload: path.join(__dirname, "preload.cjs"), sandbox: true,
                contextIsolation: true, nodeIntegration: false },
        });
        window.removeMenu();
        window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
        window.webContents.on("will-navigate", (event) => event.preventDefault());
        window.webContents.session.setPermissionRequestHandler((_contents, _permission, respond) => respond(false));
        window.webContents.session.setPermissionCheckHandler(() => false);
        window.once("ready-to-show", () => {
            if (saved.maximized) window.maximize();
            window.show();
        });
        window.on("close", (event) => {
            saveWindowState();
            if (!quitting && busy && tray) {
                event.preventDefault();
                window.hide();
            }
        });
        window.on("closed", () => { window = null; });
        tray = new Tray(icon);
        tray.setToolTip("本地字幕生成工具");
        tray.on("click", showWindow);
        tray.setContextMenu(Menu.buildFromTemplate([
            { label: "打开窗口", click: showWindow },
            { label: "彻底退出", click: quitFromTray },
        ]));
        await window.loadFile(page);
        startHandoff(initial.handoffPort);
    }).catch((error) => {
        dialog.showErrorBox("字幕工具启动失败",
            `请确认已安装 Python 3.10+，并在项目目录安装 Python 依赖。\n\n${error.message}`);
        app.quit();
    });
}

app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
    if (allowQuit) return;
    if (!backend) {
        server?.close();
        tray?.destroy();
        return;
    }
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    saveWindowState();
    server?.close();
    if (window && !window.isDestroyed()) window.hide();
    backend.stdin.end(JSON.stringify({ command: "shutdown" }) + "\n");
    backendClosed.finally(() => {
        tray?.destroy();
        allowQuit = true;
        app.quit();
    });
});
