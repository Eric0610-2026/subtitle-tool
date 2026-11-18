"use strict";

const api = window.subtitle;
const $ = (id) => document.getElementById(id);
const dialogs = Object.fromEntries([...document.querySelectorAll("dialog")].map((dialog) => [dialog.id, dialog]));
const nameOf = (path) => path.split(/[\\/]/).at(-1);
const directory = () => $("directory").value.trim();
const call = (command, ...args) => api.call(command, ...args);
const bind = (id, event, handler) => $(id).addEventListener(event, safe(handler));
const click = (id, handler) => bind(id, "click", handler);
const previewLimit = 100;

let busy = null;
let backendUp = true;
let jobs = [];
let selected = new Set();
let settings;
let maxLogs = 500;
let logs = [];
let outputPath = null;
let previewVersion = 0;
let preview = {};
let modelStates = {};
let pause = null;
let editor = null;
let offsetHandler;
let media = null;
let records = null;
let toastTimer;

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}

function safe(handler) {
    return async (event) => {
        try {
            await handler(event);
        } catch (error) {
            log(error.message, "ERROR");
            toast(error.message);
        }
    };
}

function toast(message) {
    $("toast").textContent = message;
    if (!$("toast").matches(":popover-open")) $("toast").showPopover();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toast").hidePopover(), 4500);
}

function log(message, level = "INFO", trace = "") {
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    logs.push({ time, level, message, trace });
    const list = $("logs");
    const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 18;
    const entry = element("li");
    entry.dataset.level = level;
    entry.append(element("time", time), element("span", message));
    if (trace) {
        const detail = element("details");
        detail.append(element("summary", "详细信息"), element("pre", trace));
        entry.append(detail);
    }
    list.append(entry);
    if (logs.length > maxLogs) {
        logs.shift();
        list.firstElementChild.remove();
    }
    if (follow) list.scrollTop = list.scrollHeight;
    $("export-log").disabled = false;
}

function syncButtons() {
    const idle = backendUp && !busy;
    const runnable = jobs.some((job) => !job.ignored);
    $("start").disabled = $("retry").disabled = !idle || !runnable;
    $("stop").disabled = !backendUp || !busy || busy === "model";
    $("embed").disabled = $("extract").disabled = !idle;
    $("media-start").disabled = !idle || !media?.entries.length;
    $("editor-offset").disabled = $("save-edits").disabled = !idle;
    ["open-srt", "quality", "edit-srt", "preview-offset", "preview-save"].forEach((id) => {
        $(id).disabled = !idle || (id !== "open-srt" && !preview.text);
    });
    $("show-output").disabled = !outputPath;
    const availableSelection = jobs.some((job) => selected.has(job.path) && !job.active);
    $("remove").disabled = $("ignore").disabled = !backendUp || !availableSelection;
    $("clear").disabled = !backendUp || !jobs.some((job) => !job.active);
    $("select-all").disabled = !jobs.length;
    $("selection").textContent = selected.size ? `已选 ${selected.size} 个` : "";
    $("models").disabled = !backendUp;
    updateModels();
    if (records) syncRecordButtons();
}

function renderQueue() {
    selected = new Set(jobs.filter((job) => selected.has(job.path)).map((job) => job.path));
    $("queue-count").textContent = `${jobs.length} 个文件`;
    $("empty-queue").hidden = jobs.length > 0;
    $("queue").replaceChildren(...jobs.map((job) => {
        const row = element("li", undefined, job.ignored ? "ignored" : "");
        row.draggable = true;
        row.dataset.path = job.path;
        const check = element("input");
        check.type = "checkbox";
        check.checked = selected.has(job.path);
        check.setAttribute("aria-label", `选择 ${job.name}`);
        check.addEventListener("change", () => {
            if (check.checked) selected.add(job.path);
            else selected.delete(job.path);
            syncButtons();
        });
        const file = element("button", undefined, "queue-file");
        file.title = job.path;
        file.append(element("strong", job.name, "truncate"), element("small", job.audio ? "音频" : "视频"));
        file.firstChild.classList.add("file-title");
        file.addEventListener("click", safe(async () => {
            selected = new Set([job.path]);
            $("queue").querySelectorAll("input").forEach((input) => { input.checked = input === check; });
            syncButtons();
            if (!busy) await openPreview(job.path, true);
        }));
        const info = element("span", job.active ? "处理中" : job.ignored ? "已忽略" : job.size,
            job.active ? "state" : "muted");
        row.append(check, file, info);
        return row;
    }));
    syncButtons();
}

async function addPaths(paths, checkDone = true) {
    if (!paths.length) return;
    const { added, skipped, invalid } = await call("add", paths, checkDone);
    const message = [`已添加 ${added} 个文件`, skipped && `跳过 ${skipped} 个重复或已完成文件`,
        invalid && `${invalid} 个路径没有可导入媒体`].filter(Boolean).join("，");
    log(message, added || skipped ? "INFO" : "WARNING");
}

async function chooseFolder() {
    const [path] = await api.pick("folder", directory());
    if (!path) return;
    $("directory").value = path;
    await addPaths([path]);
}

click("browse", chooseFolder);
click("add-folder", chooseFolder);
click("add-media", async () => addPaths(await api.pick("media", directory())));
click("default-dir", async () => {
    const path = settings?.default_video_dir;
    if (!path) throw new Error("请先在更多设置中填写默认视频目录");
    $("directory").value = path;
    await addPaths([path]);
});
bind("directory", "keydown", async (event) => {
    if (event.key === "Enter" && directory()) await addPaths([directory()]);
});
click("select-all", () => {
    selected = selected.size === jobs.length ? new Set() : new Set(jobs.map((job) => job.path));
    renderQueue();
});
click("remove", () => call("change_queue", "remove", [...selected]));
click("ignore", () => call("change_queue", "ignore", [...selected]));
click("clear", async () => {
    if (await api.confirm("清空待处理队列？", "正在处理的文件会保留在队列中。")) {
        await call("change_queue", "remove", jobs.map((job) => job.path));
    }
});

const containsFiles = (event) => event.dataTransfer.types.includes("Files");
let dragged = null;
let dropRow = null;
let dropAfter = false;
bind("queue", "dragstart", (event) => {
    const row = event.target.closest("li");
    if (!row) return;
    dragged = row.dataset.path;
    row.classList.add("dragged");
    event.dataTransfer.setData("application/x-subtitle-job", dragged);
    event.dataTransfer.effectAllowed = "move";
});
bind("queue", "dragend", () => {
    dragged = null;
    clearDropMarks();
});
function clearDropMarks() {
    document.querySelectorAll(".dropping, .dragged, .drop-before, .drop-after").forEach((node) => {
        node.classList.remove("dropping", "dragged", "drop-before", "drop-after");
    });
    dropRow = null;
}
bind("queue-panel", "dragover", (event) => {
    if (!containsFiles(event) && !dragged) return;
    event.preventDefault();
    if (containsFiles(event)) {
        $("queue-panel").classList.add("dropping");
        event.dataTransfer.dropEffect = "copy";
    } else {
        dropRow?.classList.remove("drop-before", "drop-after");
        dropRow = event.target.closest("li");
        const bounds = dropRow?.getBoundingClientRect();
        dropAfter = !!bounds && event.clientY > bounds.top + bounds.height / 2;
        dropRow?.classList.add(dropAfter ? "drop-after" : "drop-before");
        event.dataTransfer.dropEffect = "move";
    }
});
for (const id of ["queue-panel", "preview-panel"]) {
    bind(id, "dragleave", (event) => {
        if (!$(id).contains(event.relatedTarget)) $(id).classList.remove("dropping");
    });
}
bind("queue-panel", "drop", async (event) => {
    event.preventDefault();
    const target = event.target.closest("li")?.dataset.path;
    clearDropMarks();
    if (containsFiles(event)) {
        await addPaths([...event.dataTransfer.files].map(api.filePath).filter(Boolean), false);
    } else if (dragged && target !== dragged) {
        const order = jobs.map((job) => job.path).filter((path) => path !== dragged);
        const index = order.indexOf(target);
        order.splice(index < 0 ? order.length : index + Number(dropAfter), 0, dragged);
        await call("change_queue", "reorder", order);
    }
});
bind("preview-panel", "dragover", (event) => {
    if (!busy && containsFiles(event)) {
        event.preventDefault();
        $("preview-panel").classList.add("dropping");
    }
});
bind("preview-panel", "drop", async (event) => {
    event.preventDefault();
    clearDropMarks();
    const subtitle = [...event.dataTransfer.files].map(api.filePath).find((file) => /\.srt$/i.test(file));
    if (subtitle && !busy) await openPreview(subtitle, false);
});
document.addEventListener("dragover", (event) => { if (containsFiles(event)) event.preventDefault(); });
document.addEventListener("drop", (event) => { if (containsFiles(event)) event.preventDefault(); });

const timingPattern = /^(?:(\d+)[ \t]*\n)?[ \t]*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})[ \t]*-->[ \t]*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})[^\n]*(?:\n|$)/;

function previewRows(text, limit = previewLimit) {
    const normal = text.replace(/\r\n?/g, "\n");
    const starts = [];
    for (const match of normal.matchAll(new RegExp(timingPattern.source, "gm"))) {
        starts.push(match.index);
        if (starts.length > limit) break;
    }
    if (!starts.length) return normal.trim() ? [normal.trim()] : [];
    if (normal.slice(0, starts[0]).trim()) starts.unshift(0);
    return starts.slice(0, limit).map((start, idx) => normal.slice(start, starts[idx + 1]).trim());
}

function setPreview(text = "", path = null, { live = false, fallback = [], paired = false } = {}) {
    const frozen = live && (preview.rows?.length ?? 0) >= previewLimit;
    previewVersion++;
    preview = { text, path, fallback, paired, frozen, rows: frozen ? preview.rows : previewRows(text) };
    if (!frozen) renderPreview();
    labelPreview();
}

function renderPreview() {
    const rows = preview.rows || [];
    $("translation-heading").hidden = !preview.paired;
    $("preview-rows").replaceChildren(...rows.map((block, index) => {
        const row = element("tr");
        const header = timingPattern.exec(block);
        const text = header ? block.slice(header[0].length) : block;
        const lineBreak = preview.paired ? text.indexOf("\n") : -1;
        row.append(element("td", header?.[1] || index + 1),
            element("td", header ? `${header[2]}\n${header[3]}` : "", "time"),
            element("td", lineBreak < 0 ? text : text.slice(0, lineBreak)));
        if (preview.paired) row.append(element("td", lineBreak < 0 ? "" : text.slice(lineBreak + 1)));
        return row;
    }));
    $("preview-table").hidden = !rows.length;
    $("empty-preview").hidden = !!rows.length;
    $("preview-count").textContent = rows.length ? (rows.length === previewLimit ? "前 100 段" : `${rows.length} 段`) : "";
}

function labelPreview() {
    $("preview-file").textContent = preview.path ? nameOf(preview.path) : preview.text ? "当前预览" : "";
    $("preview-file").title = preview.path || "";
    syncButtons();
}

async function openPreview(path, isMedia) {
    const version = ++previewVersion;
    const { text, path: source } = await call("preview", path, isMedia);
    if (version === previewVersion && !busy) setPreview(text, source);
}

async function saveSubtitle(text, path) {
    if (path) {
        await call("save_preview", path, text);
        toast("字幕已保存");
        return path;
    }
    return api.save("subtitle", "字幕.srt", text);
}

click("open-srt", async () => {
    const [file] = await api.pick("subtitle", directory());
    if (file) await openPreview(file, false);
});
click("preview-save", async () => {
    const { text, path, fallback, paired } = preview;
    const saved = await saveSubtitle(text, path);
    if (saved) setPreview(text, saved, { fallback, paired });
});
click("preview-offset", () => openOffset(async (offset) => {
    const { text, path, paired } = preview;
    const shifted = await call("analyze", text, [], paired, offset);
    const saved = await saveSubtitle(shifted.text, path);
    if (saved) setPreview(shifted.text, saved, { paired });
}));

function openOffset(handler) {
    offsetHandler = handler;
    $("offset-form").reset();
    dialogs["offset-dialog"].showModal();
}

bind("offset-form", "submit", async (event) => {
    event.preventDefault();
    const value = $("offset-form").elements.offset.valueAsNumber;
    dialogs["offset-dialog"].close();
    await offsetHandler(value);
});

async function openEditor(quality, row = null) {
    const source = preview;
    const result = await call("analyze", source.text, source.fallback, source.paired);
    if (preview !== source || busy) return;
    editor = { ...source, pages: [], page: 0, size: Number($("page-size").value) };
    rebuildEditor(result.blocks, row ?? 0);
    $("find-text").value = "";
    $("issue-panel").hidden = true;
    dialogs["editor-dialog"].showModal();
    if (quality) showIssues(result.issues);
    else if (row !== null) selectBlock(row);
}

function rebuildEditor(blocks, index) {
    editor.pages = [];
    for (let i = 0; i < blocks.length; i += editor.size) {
        editor.pages.push(blocks.slice(i, i + editor.size).join("\n\n"));
    }
    if (!editor.pages.length) editor.pages.push("");
    showEditorPage(Math.floor(index / editor.size));
}

function saveEditorPage() {
    editor.pages[editor.page] = $("editor-text").value;
}

function editorText() {
    saveEditorPage();
    return editor.pages.join("\n\n");
}

function showEditorPage(index) {
    editor.page = Math.max(0, Math.min(index, editor.pages.length - 1));
    $("editor-text").value = editor.pages[editor.page];
    $("editor-page").value = editor.page + 1;
    $("editor-page").max = editor.pages.length;
    $("editor-pages").textContent = editor.pages.length;
    $("previous-page").disabled = editor.page === 0;
    $("next-page").disabled = editor.page === editor.pages.length - 1;
}

function goToEditorPage(page) {
    saveEditorPage();
    showEditorPage(page);
}

function selectEditorText(start, length) {
    const field = $("editor-text");
    field.focus();
    field.setSelectionRange(start, start + length);
    const lines = field.value.slice(0, start).split("\n").length - 1;
    field.scrollTop = Math.max(0, (lines - 2) * parseFloat(getComputedStyle(field).lineHeight));
}

function selectBlock(row) {
    const text = $("editor-text").value;
    const block = previewRows(text, editor.size)[row % editor.size];
    if (block) selectEditorText(text.indexOf(block), block.length);
}

function showIssues(issues) {
    $("issue-panel").hidden = false;
    $("issue-summary").textContent = issues.length ? `发现 ${issues.length} 处问题` : "未发现字幕质量问题";
    $("issues").replaceChildren(...issues.map(([row, number, timing, messages]) => {
        const li = element("li");
        const button = element("button", `第 ${number} 段 · ${timing} · ${messages.join("、")}`, "quiet");
        button.addEventListener("click", () => {
            showEditorPage(Math.floor(row / editor.size));
            selectBlock(row);
        });
        li.append(button);
        return li;
    }));
}

click("edit-srt", () => openEditor(false));
click("quality", () => openEditor(true));
bind("preview-rows", "dblclick", (event) => {
    const row = event.target.closest("tr");
    if (row && !busy && !preview.frozen) return openEditor(false, row.sectionRowIndex);
});
click("previous-page", () => goToEditorPage(editor.page - 1));
click("next-page", () => goToEditorPage(editor.page + 1));
bind("editor-page", "change", () => goToEditorPage($("editor-page").valueAsNumber - 1 || 0));
bind("editor-text", "input", () => { $("issue-panel").hidden = true; });
bind("page-size", "change", async () => {
    const index = editor.page * editor.size;
    const result = await call("analyze", editorText(), editor.fallback, editor.paired);
    editor.size = Number($("page-size").value);
    rebuildEditor(result.blocks, index);
});
click("find", () => {
    const needle = $("find-text").value.trim().toLowerCase();
    if (!needle) return;
    const startPage = editor.page;
    const startAt = $("editor-text").selectionEnd;
    saveEditorPage();
    for (let steps = 0; steps <= editor.pages.length; steps++) {
        const page = (startPage + steps) % editor.pages.length;
        const index = editor.pages[page].toLowerCase().indexOf(needle, steps ? 0 : startAt);
        if (index >= 0) {
            showEditorPage(page);
            selectEditorText(index, needle.length);
            return;
        }
    }
    toast("未找到匹配字幕");
});
click("editor-quality", async () => {
    const text = editorText();
    const result = await call("analyze", text, editor.fallback, editor.paired);
    if (text !== editorText()) return;
    rebuildEditor(result.blocks, editor.page * editor.size);
    showIssues(result.issues);
});
click("editor-offset", () => openOffset(async (offset) => {
    const result = await call("analyze", editorText(), [], editor.paired, offset);
    editor.fallback = [];
    rebuildEditor(result.blocks, editor.page * editor.size);
    $("issue-panel").hidden = true;
}));
click("apply-edits", () => {
    setPreview(editorText(), editor.path, { fallback: editor.fallback, paired: editor.paired });
    dialogs["editor-dialog"].close();
});
click("save-edits", async () => {
    const text = editorText();
    const path = await saveSubtitle(text, editor.path);
    if (!path) return;
    setPreview(text, path, { fallback: editor.fallback, paired: editor.paired });
    dialogs["editor-dialog"].close();
});

let glossaryDraft;
let glossaryLang;
const settingsForm = $("settings-form");
const setting = (key) => settingsForm.elements.namedItem(key);

function openSettings() {
    glossaryDraft = Object.fromEntries(Object.entries(settings.name_glossaries).map(([lang, names]) =>
        [lang, Object.entries(names).map(([from, to]) => `${from}=${to}`).join("\n")]));
    for (const [key, value] of Object.entries(settings)) {
        const control = setting(key);
        if (!control) continue;
        if (control.type === "checkbox") control.checked = value;
        else control.value = value;
    }
    glossaryLang = setting("target_lang").value;
    setting("glossary").value = glossaryDraft[glossaryLang] || "";
    setting("translation_batch_size").disabled = setting("send_all").checked;
    dialogs["settings-dialog"].showModal();
}

click("settings", openSettings);
setting("target_lang").addEventListener("change", () => {
    glossaryDraft[glossaryLang] = setting("glossary").value;
    glossaryLang = setting("target_lang").value;
    setting("glossary").value = glossaryDraft[glossaryLang] || "";
});
setting("send_all").addEventListener("change", () => {
    setting("translation_batch_size").disabled = setting("send_all").checked;
});
for (const button of document.querySelectorAll("[data-dir]")) {
    button.addEventListener("click", safe(async () => {
        const control = setting(button.dataset.dir);
        const [file] = await api.pick("folder", control.value);
        if (file) control.value = file;
    }));
}
bind("settings-form", "submit", async (event) => {
    event.preventDefault();
    const values = {};
    for (const key of Object.keys(settings)) {
        const control = setting(key);
        if (!control) continue;
        if (control.type === "checkbox") values[key] = control.checked;
        else if (control.type === "number") values[key] = control.disabled ? settings[key] : control.valueAsNumber;
        else values[key] = control.value;
    }
    glossaryDraft[glossaryLang] = setting("glossary").value;
    values.glossary_texts = glossaryDraft;
    settings = await call("save_settings", values, event.submitter?.value === "permanent");
    dialogs["settings-dialog"].close();
});

function updateModels() {
    const whisper = modelStates.whisper;
    const hy = modelStates["hy-mt2"];
    $("whisper-status").textContent = whisper ? whisper.name || "未加载" : "检查中";
    $("hy-mt2-status").textContent = !hy ? "检查中" : hy.loaded
        ? hy.owned ? "已就绪 · 本应用启动" : "已就绪 · 外部启动"
        : hy.running ? "端口已监听，服务未就绪" : "未加载";
    for (const button of document.querySelectorAll("[data-model]")) {
        const state = modelStates[button.dataset.model];
        const active = state?.loaded || state?.running;
        button.disabled = !backendUp || !!busy || !state || (button.dataset.op === "load" ? active : !active);
    }
    $("refresh-models").disabled = !backendUp || busy === "model";
}

async function refreshModels() {
    if (busy === "model" || !backendUp) return;
    modelStates = await call("model_states");
    updateModels();
}

click("models", async () => {
    $("model-detail").textContent = "";
    dialogs["model-dialog"].showModal();
    await refreshModels();
});
click("refresh-models", refreshModels);
for (const button of document.querySelectorAll("[data-model]")) {
    button.addEventListener("click", safe(async () => {
        $("model-detail").textContent = `正在${button.textContent}…`;
        await call("operate_model", button.dataset.model, button.dataset.op);
    }));
}

function openMedia(kind) {
    media = { kind, entries: [] };
    $("media-title").textContent = kind === "embed" ? "嵌入字幕" : "提取字幕";
    $("embed-fields").hidden = kind !== "embed";
    $("extract-fields").hidden = kind !== "extract";
    $("embed-video").value = $("embed-srt").value = "";
    $("convert-mp4").checked = false;
    renderMedia();
    dialogs["media-dialog"].showModal();
}

function renderMedia() {
    $("media-list").replaceChildren(...media.entries.map((entry, index) => {
        const item = element("li");
        const text = Array.isArray(entry) ? entry.map(nameOf).join(" + ") : nameOf(entry);
        const remove = element("button", "移除", "quiet");
        remove.addEventListener("click", () => {
            media.entries.splice(index, 1);
            renderMedia();
        });
        item.append(element("span", text, "grow"), remove);
        return item;
    }));
    $("media-clear").disabled = !media.entries.length;
    syncButtons();
}

click("embed", () => openMedia("embed"));
click("extract", () => openMedia("extract"));
click("pick-video", async () => {
    const [video] = await api.pick("video", directory());
    if (!video) return;
    $("embed-video").value = video;
    const match = await call("match", video);
    if (match) $("embed-srt").value = match;
});
click("pick-subtitle", async () => {
    const [subtitle] = await api.pick("subtitle", directory());
    if (!subtitle) return;
    $("embed-srt").value = subtitle;
    if (!$("embed-video").value) $("embed-video").value = await call("match", subtitle) || "";
});
click("add-pair", () => {
    const video = $("embed-video").value;
    const subtitle = $("embed-srt").value;
    if (!video || !subtitle) throw new Error("请先选择视频和字幕文件");
    if (media.entries.some(([existing]) => existing === video)) throw new Error("此视频已在列表中");
    media.entries.push([video, subtitle]);
    $("embed-video").value = $("embed-srt").value = "";
    renderMedia();
});
click("extract-pick", async () => {
    const files = await api.pick("videos", directory());
    media.entries = [...new Set([...media.entries, ...files])];
    renderMedia();
});
click("media-clear", () => {
    media.entries = [];
    renderMedia();
});
click("media-start", async () => {
    const { kind, entries } = media;
    const convert = $("convert-mp4").checked;
    const embed = kind === "embed";
    const detail = embed ? "嵌入产物通过时长验证后，源视频和 SRT 会被删除。"
        : convert ? "转为 MP4 并通过时长验证后，会删除原视频（源文件本身是 MP4 时在原位处理）。"
            : "提取每个视频的第一个内嵌字幕流，保存为 SRT。";
    if (!await api.confirm(`确认${embed ? "嵌入" : "提取"} ${entries.length} 个文件的字幕？`, detail)) return;
    outputPath = null;
    await call("start_media", kind, entries, convert);
    dialogs["media-dialog"].close();
});

const recordsPerPage = 50;

function recordEntries() {
    return records.kind === "cache" ? records.data : records.data[records.group].map((path) => [path, path]);
}

function syncRecordButtons() {
    $("records-remove").disabled = !backendUp || !!busy || !records.selected.size;
    $("records-clear").disabled = !backendUp || !!busy || !recordEntries().length;
}

function renderRecords() {
    const isCache = records.kind === "cache";
    const entries = recordEntries();
    const pages = Math.max(1, Math.ceil(entries.length / recordsPerPage));
    records.page = Math.max(0, Math.min(records.page, pages - 1));
    $("records-title").textContent = `${isCache ? "翻译缓存" : "处理历史"} · ${entries.length} 条`;
    $("record-tabs").hidden = isCache;
    $("records-clear").hidden = !isCache;
    $("records-remove").textContent = !isCache && records.group === "ignored" ? "恢复选中" : "删除选中";
    for (const tab of document.querySelectorAll("[data-records]")) {
        tab.classList.toggle("tint", tab.dataset.records === records.group);
    }
    const begin = records.page * recordsPerPage;
    $("records-list").replaceChildren(...entries.slice(begin, begin + recordsPerPage).map(([key, value]) => {
        const li = element("li");
        const check = element("input");
        check.type = "checkbox";
        check.checked = records.selected.has(key);
        const text = isCache ? value : nameOf(value);
        check.setAttribute("aria-label", `选择 ${text}`);
        check.addEventListener("change", () => {
            if (check.checked) records.selected.add(key);
            else records.selected.delete(key);
            syncRecordButtons();
        });
        const label = element("span", text, "grow");
        label.title = key;
        li.append(check, label);
        return li;
    }));
    if (!entries.length) $("records-list").append(element("li", "暂无记录", "muted"));
    $("record-page").textContent = `第 ${records.page + 1} / ${pages} 页`;
    $("records-prev").disabled = records.page === 0;
    $("records-next").disabled = records.page === pages - 1;
    syncRecordButtons();
}

async function openRecords(kind) {
    records = { kind, data: await call(kind), group: "done", page: 0, selected: new Set() };
    renderRecords();
    dialogs["records-dialog"].showModal();
}

click("history", () => openRecords("history"));
click("cache", () => openRecords("cache"));
click("records-prev", () => { records.page--; renderRecords(); });
click("records-next", () => { records.page++; renderRecords(); });
for (const tab of document.querySelectorAll("[data-records]")) {
    tab.addEventListener("click", () => {
        records.group = tab.dataset.records;
        records.page = 0;
        records.selected.clear();
        renderRecords();
    });
}
click("records-remove", async () => {
    const selectedKeys = [...records.selected];
    if (!await api.confirm(`${$("records-remove").textContent}的 ${selectedKeys.length} 条记录？`,
        "此操作只会修改记录。")) return;
    records.data = records.kind === "cache" ? await call("cache", selectedKeys)
        : await call("remove_history", records.group, selectedKeys);
    records.selected.clear();
    renderRecords();
});
click("records-clear", async () => {
    if (!await api.confirm("清空翻译缓存？", "下次处理时会重新翻译。")) return;
    records.data = await call("cache", null, true);
    records.selected.clear();
    renderRecords();
});

function showPause(event) {
    pause = event;
    $("pause-file").textContent = event.file_name;
    $("pause-text").value = event.text;
    if (!dialogs["pause-dialog"].open) dialogs["pause-dialog"].showModal();
}

async function finishPause(action) {
    if (!pause) return;
    const modified = $("pause-text").value;
    await call("finish_pause", pause.token, action, modified === pause.text ? null : modified);
}

click("skip-pause", () => finishPause("skip"));
click("embed-pause", () => finishPause("embed"));
click("stop-pause", stopTask);
bind("pause-dialog", "cancel", async (event) => {
    event.preventDefault();
    await finishPause("skip");
});
for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => dialogs[button.dataset.close].close());
}

function setProgress(id, percent) {
    const value = Math.max(0, Math.min(100, Math.round(percent || 0)));
    $(id === "overall" ? "overall" : "stage-progress").value = value;
    $(`${id}-pct`).textContent = `${value}%`;
}

function progressBatch(event) {
    setProgress("overall", 0);
    setProgress("stage", 0);
    $("stage").textContent = "当前阶段";
    $("stage-detail").textContent = "";
    $("overall-detail").textContent = event.first ? `本批 ${event.total} 个文件` : `开始处理新增的 ${event.total} 个文件`;
    $("counter").textContent = "已转写 0 / 已翻译 0 / 缓存 0";
    if (event.first) {
        outputPath = null;
        setPreview();
    }
}

function progress(event) {
    $("stage").textContent = event.stage || "处理中";
    $("stage-detail").textContent = event.detail || "";
    $("stage-detail").title = event.detail || "";
    setProgress("stage", event.percent);
    if (event.overall !== undefined) {
        setProgress("overall", event.overall);
        $("overall-detail").textContent = `第 ${event.idx} / ${event.total} 个`
            + (event.remaining && event.remaining !== "--:--" ? ` · 剩余 ${event.remaining} · 预计 ${event.eta}` : "");
    }
}

function handleEvent(event) {
    switch (event.type) {
        case "queue":
            jobs = event.jobs;
            renderQueue();
            break;
        case "busy":
            busy = event.kind;
            if (busy) {
                const names = { pipeline: "处理中", model: "模型操作中", embed: "嵌入中", extract: "提取中" };
                $("run-state").textContent = names[busy];
                $("run-state").classList.remove("error", "complete");
            } else {
                if (!["完成", "已停止", "有失败"].includes($("run-state").textContent)) $("run-state").textContent = "就绪";
                if (dialogs["model-dialog"].open) safe(refreshModels)();
            }
            syncButtons();
            break;
        case "batch": progressBatch(event); break;
        case "progress": progress(event); break;
        case "media_progress":
            setProgress("overall", 100 * event.index / event.total);
            $("overall-detail").textContent = `第 ${event.index} / ${event.total} 个`;
            break;
        case "counter":
            $("counter").textContent = `已转写 ${event.generated}/${event.total} · 已翻译 ${event.translated}/${event.total} · 缓存 ${event.cache}`;
            break;
        case "current":
        case "language":
            $("stage-detail").textContent = event.message;
            $("stage-detail").title = event.message;
            break;
        case "model_progress": $("model-detail").textContent = event.message; break;
        case "models":
            modelStates = event.states;
            $("model-detail").textContent = "";
            updateModels();
            break;
        case "preview_clear": setPreview(); break;
        case "preview_append":
            if ((preview.rows?.length ?? 0) < previewLimit) {
                setPreview([preview.text, event.message].filter(Boolean).join("\n\n"));
            }
            break;
        case "preview":
            setPreview(event.message, event.source_path, { live: true, paired: event.paired_preview, fallback: event.fallback_rows });
            break;
        case "output_path":
            outputPath = event.path;
            $("show-output").title = outputPath;
            syncButtons();
            break;
        case "pause_before_embed": showPause(event); break;
        case "pause_closed":
            pause = null;
            dialogs["pause-dialog"].close();
            break;
        case "log":
            log(event.message, event.level, event.trace);
            if (busy === "model") $("model-detail").textContent = event.message;
            break;
        case "done":
            log(event.message + (event.elapsed ? ` · 耗时 ${event.elapsed} · ${event.files} 个文件` : ""),
                event.stopped || !event.failed ? "INFO" : "WARNING");
            if (!event.stopped && !event.failed) setProgress("overall", 100);
            $("overall-detail").textContent = event.message;
            $("run-state").textContent = event.stopped ? "已停止" : event.failed ? "有失败" : "完成";
            $("run-state").classList.toggle("error", !event.stopped && !!event.failed);
            $("run-state").classList.toggle("complete", !event.stopped && !event.failed);
            break;
        case "error":
            log(event.message, "ERROR", event.trace);
            $("overall-detail").textContent = event.message;
            $("run-state").textContent = "有失败";
            $("run-state").classList.add("error");
            if (busy === "model") $("model-detail").textContent = event.message;
            toast(event.message);
            break;
        case "backend_closed":
            backendUp = false;
            busy = null;
            $("run-state").textContent = "后端已退出";
            $("run-state").classList.add("error");
            log("字幕处理后端已退出，请重启程序", "ERROR", event.message);
            syncButtons();
            break;
    }
}

click("theme", () => api.theme());
click("show-output", () => api.reveal(outputPath));
click("start", () => call("start_processing", $("translate").checked, false));
click("retry", () => call("start_processing", $("translate").checked, true));
async function stopTask() {
    if (!await api.confirm("停止当前任务？", "已写入的检查点会保留。")) return;
    await call("stop");
    log("已请求停止");
}
click("stop", stopTask);
click("export-log", async () => {
    const now = new Date();
    const content = [`本地字幕生成工具 · ${now.toLocaleString("zh-CN")}`, "",
        ...logs.map(({ time, level, message, trace }) =>
            `[${time}] [${level}] ${message}${trace ? `\n${trace}` : ""}`)].join("\n");
    const name = `subtitle-log-${now.toISOString().slice(0, 10)}.txt`;
    const path = await api.save("log", name, content);
    if (path) toast("日志已导出");
});

api.onEvent(handleEvent);
safe(async () => {
    const state = await call("initial_state");
    settings = state.settings;
    jobs = state.jobs;
    busy = state.busy;
    maxLogs = state.maxLogs;
    const colorNames = { bg: "bg", card: "surface", header: "header", text: "ink", text_sec: "muted",
        text_muted: "placeholder", border: "border", accent: "accent", success: "success", danger: "danger" };
    for (const [mode, overrides] of Object.entries(state.themeOverrides)) {
        for (const [key, color] of Object.entries(overrides)) {
            if (Object.hasOwn(colorNames, key) && CSS.supports("color", color)) {
                document.documentElement.style.setProperty(`--${mode}-${colorNames[key]}`, color);
            }
        }
    }
    const options = Object.entries(state.languages);
    for (const [id, entries] of [["language", [["auto", "自动检测"], ...options]], ["target_lang", options]]) {
        setting(id).replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
    }
    setPreview();
    renderQueue();
    log("应用就绪");
    for (const message of state.warnings) log(message, "WARNING");
})();
