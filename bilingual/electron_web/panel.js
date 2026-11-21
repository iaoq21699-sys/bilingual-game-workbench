function mount(options, mountTranslations) {
    "use strict";
    if (location.protocol !== "file:" || location.pathname !== options.path || !document.body) return null;
    if (options.borneo && !document.getElementById("app")) return null;
    const app = document.body;
    if (globalThis.bilingualWorkbench) return globalThis.bilingualWorkbench.status();

    const host = document.createElement("div");
    host.id = "bilingual-workbench-panel";
    const root = host.attachShadow({mode: "closed"});
    const panelSheet = new CSSStyleSheet();
    panelSheet.replaceSync(options.css);
    root.adoptedStyleSheets = [panelSheet];
    root.innerHTML = `
        <section class="expanded" aria-label="双语阅读" hidden>
            <header><div class="heading"><span class="book-emblem" aria-hidden="true"></span><strong>双语阅读</strong><span id="game-title"></span></div><div class="header-actions">
                <div class="layouts" role="group" aria-label="面板大小">
                    <button id="large" type="button" aria-pressed="true">大面板</button>
                    <button id="compact" type="button" aria-pressed="false">小面板</button>
                </div>
                <button id="pin" type="button" disabled>固定阅读</button>
                <button id="appearance" type="button" aria-expanded="false" aria-controls="appearance-controls">外观</button>
                <button id="close" type="button">收起 <kbd>Esc</kbd></button>
            </div></header>
            <div id="appearance-controls" role="region" aria-label="当前游戏外观" hidden>
                <div class="appearance-grid">
                    <label class="opacity-control" for="opacity"><span>背景不透明度 <output id="opacity-value" for="opacity"></output></span>
                        <input id="opacity" type="range" step="1">
                    </label>
                    <label for="preset">主题预设<select id="preset"><option value="" disabled>当前配色</option></select></label>
                    <label for="card-side">小卡位置<select id="card-side"><option value="left">靠左</option><option value="right">靠右</option></select></label>
                </div>
                <div class="appearance-actions">
                    <button id="save-appearance" type="button">保存外观</button>
                    <button id="close-appearance" type="button">关闭</button>
                    <span id="appearance-status" role="status"></span>
                </div>
            </div>
            <div class="panel-body">
                <div id="search-tools">
                    <label for="query" class="search-label">查找原句</label>
                    <input id="query" placeholder="输入英文或中文片段" autocomplete="off" maxlength="240">
                    <div class="filters">
                        <select id="category" aria-label="文本类别"><option value="">全部类别</option></select>
                        <button id="clear" type="button" disabled>清空</button>
                    </div>
                    <div class="search-meta">
                        <span id="count" role="status"></span>
                        <label><input id="inline" type="checkbox"> 原位双语</label>
                    </div>
                    <div id="results" aria-label="检索结果" hidden></div>
                    <nav id="pages" aria-label="结果翻页" hidden><button id="prev" type="button">上一页</button>
                        <span id="page-number"></span><button id="next" type="button">下一页</button></nav>
                </div>
                <div id="reading">
                    <p id="reading-empty">选中左侧原句查看中英对照与释义。</p>
                    <article hidden>
                        <p id="source"></p>
                        <div id="english" lang="en"></div>
                        <details id="translation" open><summary>中文对照</summary><p id="chinese" lang="zh-CN"></p></details>
                        <div id="definition" hidden><strong id="term"></strong><p id="meaning"></p><small id="origin"></small></div>
                        <div class="notes"><button id="note" type="button">记到笔记</button>
                            <button id="sentence" type="button" hidden>另记整句</button><span id="note-target">整句</span>
                            <span id="receipt" role="status"></span></div>
                    </article>
                </div>
            </div>
            <footer><kbd>F8</kbd> <span id="hint">开合 · Shift 点击可选词组</span></footer>
        </section>`;

    const $ = (id) => root.getElementById(id);
    const section = root.querySelector("section");
    section.classList.toggle("borneo", options.borneo);
    const article = root.querySelector("article");
    const query = $("query");
    const category = $("category");
    const results = $("results");
    const count = $("count");
    const english = $("english");
    const chinese = $("chinese");
    const translationDetails = $("translation");
    const translationButton = translationDetails.querySelector("summary");
    const notes = article.querySelector(".notes");
    const inline = $("inline");
    const definition = $("definition");
    const receipt = $("receipt");
    const panelStatus = (text) => { count.textContent = text; };
    const appearanceButton = $("appearance");
    const appearanceControls = $("appearance-controls");
    const opacitySlider = $("opacity");
    const preset = $("preset");
    const cardSide = $("card-side");
    const appearanceStatus = $("appearance-status");
    const saveAppearance = $("save-appearance");

    [opacitySlider.min, opacitySlider.max] = options.opacityRange.map(String);
    for (const [id, theme] of Object.entries(options.presets)) preset.append(new Option(theme.Name, id));
    $("game-title").textContent = options.title;
    $("game-title").title = options.title;
    for (const name of options.categories) category.append(new Option(name, name));
    panelStatus("输入当前看到的片段开始检索");

    let mode = "hidden";
    let previousFocus = null;
    let entry = null;
    let words = [];
    let anchor = -1;
    let selected = "";
    let serial = 0;
    let timer;
    let searchRevision = 0;
    let wordRevision = 0;
    let appearance = {theme: {...options.theme}, cardSide: options.cardSide === "left" ? "left" : "right"};
    let appearanceRevision = 0;
    let savedAppearanceRevision = 0;
    let nativeReading = false;
    const pending = new Map();

    function previewAppearance() {
        for (const key of ["Background", "Panel", "Text", "Muted", "Accent", "Border", "Opacity"]) {
            section.style.setProperty(`--${key.toLowerCase()}`, appearance.theme[key]);
        }
        opacitySlider.value = String(Math.round(appearance.theme.Opacity * 100));
        $("opacity-value").textContent = `${opacitySlider.value}%`;
        preset.value = Object.keys(options.presets).find((id) => `preset:${id}` === appearance.theme.Source) || "";
        cardSide.value = appearance.cardSide;
        section.classList.toggle("dock-left", appearance.cardSide === "left");
        appearanceButton.textContent = appearanceRevision === savedAppearanceRevision ? "外观" : "外观 *";
    }

    function editAppearance() {
        appearanceRevision++;
        appearanceStatus.textContent = "已预览，待保存";
        previewAppearance();
    }

    function showAppearance(open, focus = true) {
        appearanceControls.hidden = !open;
        appearanceButton.setAttribute("aria-expanded", String(open));
        if (focus) (open ? opacitySlider : appearanceButton).focus({preventScroll: true});
    }

    async function commitAppearance() {
        const revision = appearanceRevision;
        saveAppearance.disabled = true;
        appearanceStatus.textContent = "正在保存…";
        try {
            const saved = await request("appearance", appearance);
            savedAppearanceRevision = revision;
            const latest = appearanceRevision === revision;
            if (latest) appearance = saved;
            appearanceStatus.textContent = latest ? "已保存，下次启动沿用" : "已保存上次调整，当前预览待保存";
            previewAppearance();
        } catch (error) {
            appearanceStatus.textContent = `保存失败：${error.message}`;
        } finally {
            saveAppearance.disabled = false;
        }
    }

    previewAppearance();

    function request(action, args) {
        return new Promise((resolve, reject) => {
            const id = ++serial;
            const timeout = setTimeout(() => {
                pending.delete(id);
                reject(new Error("工作台响应超时，请重试。"));
            }, 6000);
            pending.set(id, {resolve, reject, timeout});
            globalThis.bilingualWorkbenchRequest(JSON.stringify({id, action, ...args}));
        });
    }

    function reply(id, response) {
        const task = pending.get(id);
        if (!task) return;
        pending.delete(id);
        clearTimeout(task.timeout);
        if (response.error) task.reject(new Error(response.error));
        else task.resolve(response.value);
    }

    function setMode(next) {
        if (next === "open") {
            previousFocus = document.activeElement;
            section.hidden = false;
            section.classList.remove("pinned");
            query.focus({preventScroll: true});
        } else {
            showAppearance(false, false);
            section.hidden = next === "hidden";
            section.classList.toggle("pinned", next === "pinned");
            root.activeElement?.blur();
            previousFocus?.focus({preventScroll: true});
        }
        mode = next;
        arrangeReading();
    }

    function onKey(event) {
        if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key !== "F8" && (event.key !== "Escape" || mode !== "open")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.repeat) return;
        if (event.key === "Escape" && !appearanceControls.hidden) {
            showAppearance(false);
            return;
        }
        setMode(event.key === "Escape" || mode === "open" ? "hidden" : "open");
    }

    function searchPattern(queryText) {
        const tokens = queryText.trim().split(/\s+/).filter(Boolean).slice(0, 12);
        if (!tokens.length) return null;
        const escaped = tokens.map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
        const terms = [...new Set(escaped)].sort((left, right) => right.length - left.length);
        if (escaped.length > 1) terms.unshift(escaped.join("\\s+"));
        return new RegExp(terms.join("|"), "giu");
    }

    function addMatches(element, text, pattern, excerpt = false) {
        if (excerpt) {
            const first = pattern ? text.matchAll(pattern).next().value : undefined;
            const start = Math.max(0, (first?.index || 0) - 18);
            const end = start + Math.max(220, (first?.[0].length || 0) + 24);
            text = `${start ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
        }
        if (!pattern) {
            element.textContent = text;
            return;
        }
        let end = 0;
        for (const match of text.matchAll(pattern)) {
            element.append(text.slice(end, match.index));
            const mark = document.createElement("mark");
            mark.textContent = match[0];
            element.append(mark);
            end = match.index + match[0].length;
        }
        element.append(text.slice(end));
    }

    function revealMatch(container, selector) {
        container.scrollTop = 0;
        const hit = container.querySelector(selector)?.getBoundingClientRect();
        const viewport = container.getBoundingClientRect();
        if (hit && viewport.height && hit.bottom > viewport.bottom) {
            container.scrollTop = hit.top - viewport.top - 8;
        }
    }

    function openEntry(item, pattern) {
        entry = item;
        article.hidden = false;
        $("reading-empty").hidden = true;
        $("reading").scrollTop = 0;
        english.replaceChildren();
        english.scrollTop = 0;
        words = [];
        anchor = -1;
        selected = "";
        wordRevision++;
        $("pin").disabled = false;
        definition.hidden = true;
        $("sentence").hidden = true;
        $("note-target").textContent = "整句";
        receipt.textContent = "";
        $("source").textContent = [item.category, item.speaker].filter(Boolean).join(" · ");
        chinese.replaceChildren();
        addMatches(chinese, item.zh, pattern);
        const hits = pattern ? [...item.en.matchAll(pattern)] : [];
        let nextHit = 0;
        let end = 0;
        for (const match of item.en.matchAll(/[A-Za-z]+(?:['’-][A-Za-z]+)*/g)) {
            english.append(item.en.slice(end, match.index));
            const button = document.createElement("button");
            button.type = "button";
            button.className = "word";
            button.textContent = match[0];
            const wordEnd = match.index + match[0].length;
            while (nextHit < hits.length && hits[nextHit].index + hits[nextHit][0].length <= match.index) nextHit++;
            if (nextHit < hits.length && hits[nextHit].index < wordEnd) button.classList.add("query-hit");
            button.setAttribute("aria-pressed", "false");
            const index = words.push(button) - 1;
            button.addEventListener("click", (event) => pickWord(index, event.shiftKey));
            english.append(button);
            end = wordEnd;
        }
        english.append(item.en.slice(end));
        revealMatch(english, ".query-hit");
        revealMatch(chinese, "mark");
    }

    async function pickWord(index, extend) {
        if (!extend || anchor < 0) anchor = index;
        const start = Math.min(anchor, index);
        const finish = Math.max(anchor, index);
        selected = words.slice(start, finish + 1).map((button) => button.textContent).join(" ");
        $("note-target").textContent = selected;
        words.forEach((button, position) => button.setAttribute("aria-pressed", String(position >= start && position <= finish)));
        $("term").textContent = selected;
        $("meaning").textContent = "正在查询…";
        $("origin").textContent = "";
        definition.hidden = false;
        $("sentence").hidden = false;
        receipt.textContent = "";
        const revision = ++wordRevision;
        try {
            const result = await request("lookup", {entry: entry.id, term: selected});
            if (revision !== wordRevision) return;
            $("meaning").textContent = result.meaning;
            $("origin").textContent = result.source || "原句注释";
        } catch (error) {
            if (revision === wordRevision) $("meaning").textContent = error.message;
        }
    }

    async function saveNote(whole) {
        if (!entry) return;
        const id = entry.id;
        receipt.textContent = "正在保存…";
        $("note").disabled = $("sentence").disabled = true;
        try {
            const result = await request("note", {entry: id, term: whole ? "" : selected});
            if (entry?.id === id) receipt.textContent = result.saved ? "已记下" : "已记过";
        } catch (error) {
            if (entry?.id === id) receipt.textContent = error.message;
        } finally {
            $("note").disabled = $("sentence").disabled = false;
        }
    }

    async function search(value, filter, start, revision) {
        panelStatus("正在检索…");
        try {
            const result = await request("search", {query: value, category: filter, offset: start});
            if (revision !== searchRevision) return;
            const pattern = searchPattern(value);
            panelStatus(result.total ? `${result.total} 条原句` : "没有匹配的原句");
            result.items.forEach((item) => {
                const button = document.createElement("button");
                button.type = "button";
                for (const [name, text] of [["result-source", item.category], ["result-en", item.en], ["result-zh", item.zh]]) {
                    if (!text) continue;
                    const label = document.createElement("span");
                    label.className = name;
                    addMatches(label, text.replace(/\s+/g, " ").trim(), name === "result-source" ? null : pattern, true);
                    button.append(label);
                }
                button.addEventListener("click", () => selectResult(button, item, pattern));
                results.append(button);
            });
            results.hidden = !result.items.length;
            if (result.items.length) selectResult(results.firstElementChild, result.items[0], pattern);
            const pages = Math.ceil(result.total / result.pageSize);
            $("pages").hidden = pages === 0;
            $("pages").classList.toggle("single-page", pages === 1);
            $("prev").disabled = start === 0;
            $("next").disabled = start + result.pageSize >= result.total;
            $("prev").dataset.start = String(Math.max(0, start - result.pageSize));
            $("next").dataset.start = String(start + result.pageSize);
            $("page-number").textContent = `${Math.floor(start / result.pageSize) + 1} / ${pages}`;
        } catch (error) {
            if (revision === searchRevision) panelStatus(error.message);
        }
    }

    function selectResult(button, item, pattern) {
        results.querySelector(".active")?.classList.remove("active");
        button.classList.add("active");
        openEntry(item, pattern);
    }

    function scheduleSearch(start = 0, delay = 0) {
        clearTimeout(timer);
        const revision = ++searchRevision;
        const value = query.value.trim();
        const filter = category.value;
        results.replaceChildren();
        results.hidden = true;
        article.hidden = true;
        $("reading-empty").hidden = false;
        $("pages").hidden = true;
        $("clear").disabled = !value && !filter;
        entry = null;
        wordRevision++;
        $("pin").disabled = true;
        if (!value && !filter) panelStatus("输入当前看到的片段开始检索");
        else timer = setTimeout(() => search(value, filter, start, revision), delay);
    }

    function setLayout(expanded) {
        section.classList.toggle("expanded", expanded);
        $("large").setAttribute("aria-pressed", String(expanded));
        $("compact").setAttribute("aria-pressed", String(!expanded));
        arrangeReading();
    }

    function describeTranslation() {
        translationButton.textContent = nativeReading ? (translationDetails.open ? "收起译文" : "展开译文") : "中文对照";
    }

    function arrangeReading() {
        const large = section.matches(".borneo.expanded:not(.pinned)");
        if (large !== nativeReading) {
            nativeReading = large;
            if (large) {
                article.prepend(notes);
                translationDetails.before(definition);
            } else {
                article.append(notes);
                translationDetails.after(definition);
            }
        }
        describeTranslation();
        $("hint").textContent = mode === "pinned" ? (section.classList.contains("expanded") ? "返回大面板" : "返回小面板") :
            large ? "点击英文查词 · 按住 Shift 点末词选词组 · F8 返回游戏" : "开合 · Shift 点击可选词组";
    }

    translationDetails.addEventListener("toggle", describeTranslation);
    arrangeReading();

    query.addEventListener("input", (event) => { if (!event.isComposing) scheduleSearch(0, 170); });
    query.addEventListener("compositionend", () => scheduleSearch(0, 170));
    category.addEventListener("change", () => scheduleSearch());
    $("clear").addEventListener("click", () => {
        query.value = category.value = "";
        scheduleSearch();
        query.focus();
    });
    for (const button of [$("prev"), $("next")]) {
        button.addEventListener("click", () => scheduleSearch(Number(button.dataset.start)));
    }
    query.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" && !event.isComposing && results.firstElementChild) {
            event.preventDefault();
            results.firstElementChild.focus();
        }
    });
    $("close").addEventListener("click", () => setMode("hidden"));
    appearanceButton.addEventListener("click", () => showAppearance(appearanceControls.hidden));
    $("close-appearance").addEventListener("click", () => showAppearance(false));
    opacitySlider.addEventListener("input", () => {
        appearance.theme.Opacity = Number(opacitySlider.value) / 100;
        editAppearance();
    });
    preset.addEventListener("change", () => {
        appearance.theme = {...options.presets[preset.value], Opacity: appearance.theme.Opacity};
        editAppearance();
    });
    cardSide.addEventListener("change", () => {
        appearance.cardSide = cardSide.value;
        editAppearance();
    });
    saveAppearance.addEventListener("click", commitAppearance);
    $("large").addEventListener("click", () => setLayout(true));
    $("compact").addEventListener("click", () => setLayout(false));
    $("pin").addEventListener("click", () => setMode("pinned"));
    $("note").addEventListener("click", () => saveNote(false));
    $("sentence").addEventListener("click", () => saveNote(true));
    for (const type of ["keydown", "keyup", "keypress", "pointerdown", "pointerup", "click", "wheel"]) {
        root.addEventListener(type, (event) => event.stopPropagation());
    }
    window.addEventListener("keydown", onKey, {capture: true});
    document.documentElement.append(host);

    const translation = mountTranslations(app, options);
    inline.checked = options.inline;
    inline.disabled = !options.inline;
    inline.parentElement.title = options.inlineLimited ? "大型语料仍可检索。原位双语仅显示已确认的界面词条。" : "显示已配对的游戏文字";
    inline.addEventListener("change", () => translation.setEnabled(inline.checked));
    translation.setEnabled(inline.checked);

    function status() {
        return host.isConnected && getComputedStyle(host).position === "fixed" ? {
            drawn: !section.hidden && section.getClientRects().length > 0,
            annotations: translation.count()
        } : null;
    }
    globalThis.bilingualWorkbench = {reply, status};
    return status();
}
