function mountTranslations(app, options) {
    "use strict";
    const attribute = "data-bilingual-workbench-translation";
    const formatAttribute = "data-bilingual-workbench-format";
    const columnsAttribute = "data-bilingual-workbench-columns";
    const gameSheet = new CSSStyleSheet();
    gameSheet.replaceSync(options.inlineCss);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, gameSheet];

    const reading = options.reading || {};
    const entities = reading.entities || {};
    const reference = /&(p\d+)(?:_(firstname|lastname|title))?(?![\w])|&([a-z][a-z0-9]+)\b/gi;
    const entityName = ([, person, field, other]) => [person || other, ({firstname: "firstName", lastname: "lastName"})[field] || field || "name"];
    const norm = (text) => text.replace(/[’‘]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, " ").trim();
    const speakerName = (text) => norm(text).replace(/[:：]\s*$/, "");
    const sourceIndex = (locale) => locale === "en" ? 0 : 1;
    const otherLanguage = (locale) => locale === "en" ? "zh-CN" : "en";

    function expand(text, index, hidden = new Set()) {
        return text.replace(reference, (...match) => {
            const [id, field] = entityName(match);
            const row = entities[id.toLowerCase()]?.[index];
            if (!row) return match[0];
            if (hidden.has(id) && field !== "title") return row.strangername || match[0];
            return row[field] || match[0];
        });
    }

    function indexPairs(pairs) {
        const maps = [new Map(), new Map()];
        for (const pair of pairs) {
            const values = pair.slice(0, 2).map((value, i) => expand(value, i));
            const normalized = values.map(norm);
            if (!normalized.every(Boolean) || normalized[0] === normalized[1]) continue;
            for (const i of [0, 1]) {
                const old = maps[i].get(normalized[i]);
                maps[i].set(normalized[i], old === undefined || (old && norm(old) === normalized[1 - i]) ? values[1 - i] : null);
            }
        }
        return maps;
    }

    const facts = [...options.pairs, ...Object.values(options.labels), ...(reading.names || []), ...(reading.documents || [])];
    const echo = reading.templates?.partialEcho;
    if (echo?.every((text) => text.includes("__ITEM__"))) {
        for (const pair of reading.names) facts.push(echo.map((template, i) => template.replace("__ITEM__", pair[i])));
    }
    const timelinePrefix = options.labels.guest_timeline_prefix;
    if (timelinePrefix && reading.names) {
        for (const pair of reading.names) facts.push(timelinePrefix.map((prefix, i) => `${prefix} ${pair[i]}`));
    }
    const exact = indexPairs(facts);
    const placeNames = indexPairs(reading.places || []);
    const dates = [...(reading.dates || [])];
    if (options.borneo) {
        const formatters = ["en-US", "zh-CN"].map((language) => new Intl.DateTimeFormat(language,
            {weekday: "long", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC"}));
        for (let hour = 0; hour < 7 * 24; hour++) {
            const value = Date.UTC(1938, 10, 15, hour);
            dates.push(formatters.map((formatter) => formatter.format(value)));
        }
        for (const key of ["time_day_prefix", "time_entry_prefix"]) {
            const labels = options.labels[key];
            if (!labels) continue;
            for (let number = 1; number <= 60; number++) dates.push(labels.map((label) => `${label} ${number}`));
        }
    }
    const timeLabels = indexPairs(dates);
    const clues = [new Map(), new Map()];
    for (const [scene, slots] of Object.entries(reading.clues || {})) {
        for (const row of slots) {
            for (const i of [0, 1]) clues[i].set(`${scene}/${row[i]}`, row);
        }
    }
    const voice = [new Map(), new Map()];
    for (const [speakers, translations] of reading.dialogue || []) {
        for (let side = 0; side < translations.length; side++) {
            const i = side % 2;
            const key = `${speakerName(speakers[i])}\0${norm(expand(translations[side], i))}`;
            const candidate = expand(translations[side + (i === 0 ? 1 : -1)], 1 - i);
            const old = voice[i].get(key);
            voice[i].set(key, old === undefined || (old && norm(old) === norm(candidate)) ? candidate : null);
        }
    }
    const speakers = indexPairs((reading.dialogue || []).map(([names]) => names));

    const rich = ".letter-body,.intro-text";
    const subtitles = "#dialogue-text,.dialogue-text,.transcript-text";
    const speakerNodes = "#dialogue-speaker-name,.speaker-name,.transcript-speaker";
    const passages = ".clue-narration-line,.clue-text,#annotation-original-text,#cw-text,#diff-desc,.item-desc";
    const ordinary = "p,h1,h2,h3,h4,label,li,button,blockquote";
    const selectors = options.borneo ? `${ordinary},[data-i18n],[data-i18n-html],${options.selectors}` : ordinary;
    const fixed = Object.entries(reading.fixed || {});
    const annotations = new Map();

    function textsOf(node) {
        const plain = norm(node.textContent);
        const multiline = node.childElementCount > 0 && !node.matches("#dialogue-speaker-name") ? norm(node.innerText) : plain;
        return multiline && plain !== multiline ? [plain, multiline] : [plain];
    }

    function fromPair(pair, texts, i, allowStranger = false) {
        if (!pair) return null;
        const censored = texts.some((text) => text.includes("@$*!"));
        if (censored && pair.length !== 4) return null;
        const base = censored ? 2 : 0;
        const source = pair[base + i];
        const target = pair[base + 1 - i];
        if (texts.includes(norm(expand(source, i)))) return expand(target, 1 - i);
        if (!allowStranger) return null;
        const ids = [...new Set([...source.matchAll(reference)].map((m) => m[1]).filter((id) => entities[id]?.[i].strangername))];
        if (ids.length > 5) return null;
        const targetIds = new Set([...target.matchAll(reference)].map((m) => m[1]));
        let match = null;
        for (let mask = 1; mask < 2 ** ids.length; mask++) {
            const hidden = new Set(ids.filter((_, bit) => mask & (1 << bit)));
            if (![...hidden].every((id) => targetIds.has(id) && entities[id][1 - i].strangername)) continue;
            if (!texts.includes(norm(expand(source, i, hidden)))) continue;
            const value = expand(target, 1 - i, hidden);
            if (match !== null && norm(match) !== norm(value)) return null;
            match = value;
        }
        return match;
    }

    function knownLabel(node, texts, locale) {
        const key = node.getAttribute("data-i18n") || node.getAttribute("data-i18n-html");
        const known = key && Object.hasOwn(options.labels, key) ? options.labels[key] : null;
        const overrides = fixed.filter(([selector]) => node.matches(selector)).map(([, pair]) => pair);
        if (known) overrides.unshift(known);
        return overrides.flatMap((pair) => [locale, locale === "en" ? "zh" : "en"].map((lang) =>
            ({text: fromPair(pair, texts, sourceIndex(lang)), locale: lang}))).find(({text}) => text);
    }

    function findText(node, locale) {
        const texts = textsOf(node);
        if (!texts[0] && texts.length === 1) return null;
        const i = sourceIndex(locale);
        const main = reading.clues && node.closest(".clue-narration-line[onclick],.clue-dialogue-line[onclick]");
        if (main && node.matches(".clue-narration-line,.dialogue-text")) {
            // Only parse the fixed room/time/slot prefix, never the handler or
            // the quoted game text after it.
            const key = /^openAnnotationModal\('([a-z0-9_]+)',\s*(\d+),\s*(\d+),/i.exec(main.getAttribute("onclick"));
            if (key) {
                const row = clues[i].get(`${key[1]}_${key[2]}/${key[3]}`);
                return {text: fromPair(row?.[3], texts, i, row?.[2] === "note" || row?.[2] === "narration"), locale};
            }
        }
        const label = knownLabel(node, texts, locale);
        if (label) return label;

        if (options.borneo && node.matches(".room-label")) {
            const id = node.closest(".room-hotspot[data-id]")?.dataset.id;
            const place = entities[id];
            if (place?.every((row) => row.name)) return {text: fromPair(place.map((row) => row.name), texts, i), locale};
        }

        if (options.borneo && node.matches("#dialogue-text,.transcript-text")) {
            const speaker = node.id === "dialogue-text" ? document.getElementById("dialogue-speaker-name") : node.parentElement.querySelector(".transcript-speaker");
            const key = speakerName(speaker?.textContent || "");
            const text = texts.map((value) => voice[i].get(`${key}\0${value}`)).find(Boolean);
            if (text) return {text, locale};
        }
        if (options.borneo && node.matches(speakerNodes)) {
            const key = speakerName(texts[0]);
            const text = speakers[i].get(key) || exact[i].get(key);
            if (text) return {text, locale};
        }
        const baseTexts = node.id === "current-location" ? texts.map((value) => value.replace(/\s*▼$/, "")) : texts;
        const text = baseTexts.map((value) => (node.id === "current-location" && placeNames[i].get(value)) ||
            exact[i].get(value) || (node.id === "current-time-display" && timeLabels[i].get(value))).find(Boolean);
        return {text, locale};
    }

    function describe(node, locale) {
        if (node.closest("[data-bilingual-workbench-reading],[contenteditable],svg,textarea,select,[aria-hidden='true']") ||
                node.querySelector("button,input,select,textarea")) return null;
        const detailed = options.borneo && node.matches(options.selectors);
        if (!detailed && node.querySelector("a,p,li,h1,h2,h3,h4")) return null;
        if (!node.getClientRects().length || getComputedStyle(node).visibility !== "visible") return null;
        const result = findText(node, locale);
        if (!result?.text || textsOf(node).includes(norm(result.text))) return null;
        const columns = options.borneo && node.matches(rich) && node.parentElement.matches(".letter-container,.intro-content");
        if (!columns && !annotations.has(node) && !["none", "normal"].includes(getComputedStyle(node, "::after").content)) return null;
        let format = "compact";
        if (detailed) {
            if (node.matches(subtitles)) format = "subtitle";
            else if (node.matches(speakerNodes)) format = "speaker";
            else if (node.matches(passages)) format = "passage";
        }
        return {...result, columns, format};
    }

    function remove(node, companion) {
        if (companion) {
            companion.parentElement?.removeAttribute(columnsAttribute);
            companion.remove();
        }
        node.removeAttribute(attribute);
        node.removeAttribute(formatAttribute);
        annotations.delete(node);
    }

    function annotate(node, result) {
        let companion = annotations.get(node);
        if (result.columns) {
            if (!companion?.isConnected) {
                companion = document.createElement("aside");
                companion.setAttribute("data-bilingual-workbench-reading", "");
                node.after(companion);
                node.parentElement.setAttribute(columnsAttribute, "");
            }
            companion.lang = otherLanguage(result.locale);
            if (companion.textContent !== result.text) companion.textContent = result.text;
        } else {
            if (node.getAttribute(attribute) !== result.text) node.setAttribute(attribute, result.text);
            if (node.getAttribute(formatAttribute) !== result.format) node.setAttribute(formatAttribute, result.format);
        }
        annotations.set(node, companion || null);
    }

    let enabled = false;
    let scanScheduled = false;
    function scan() {
        scanScheduled = false;
        const lang = document.documentElement.lang.toLowerCase();
        const locale = !lang || /^en(?:$|-)/.test(lang) ? "en" : /^(?:zh|cn)(?:$|-)/.test(lang) ? "zh" : "";
        const updates = new Map();
        if (enabled && locale) {
            for (const node of app.querySelectorAll(selectors)) {
                let parent = node.parentElement;
                while (parent && !updates.has(parent)) parent = parent.parentElement;
                if (parent) continue;
                const result = describe(node, locale);
                if (result) updates.set(node, result);
            }
        }
        for (const [node, companion] of annotations) {
            if (!updates.has(node)) remove(node, companion);
        }
        for (const [node, result] of updates) annotate(node, result);
    }

    function scheduleScan() {
        if (!enabled || scanScheduled) return;
        scanScheduled = true;
        setTimeout(scan, 85);
    }

    if (options.inline) {
        new MutationObserver(scheduleScan).observe(app, {
            subtree: true, childList: true, characterData: true, attributes: true,
            attributeFilter: ["class", "hidden", "aria-hidden", "data-i18n", "data-i18n-html"]
        });
        new MutationObserver(scheduleScan).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
        window.addEventListener("resize", scheduleScan);
    }
    return {setEnabled(value) { enabled = Boolean(value); scan(); }, count: () => annotations.size};
}
