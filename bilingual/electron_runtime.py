"""One private helper per game session. All game-page assets are bundled locally."""
from __future__ import annotations

from contextlib import suppress
import html
import json
import os
import re
from subprocess import TimeoutExpired
import sys
import time
from urllib.parse import urlsplit

from . import electron_panel, native_overlay, themes
from .core import Service, TOKENS, display_text
from .electron_pipe import ElectronPipe, PipeClosed, ProtocolError
from .game_session import GameSession
from .notes import Notebook
from .patching import save_json

WORLD = "BilingualWorkbench"
BINDING = "bilingualWorkbenchRequest"


def _text(value):
    text = display_text(value)
    separator = r"<(?:br\b[^>]*|/(?:p|div|li|tr|h[1-6])\s*)>"
    if re.search(separator, text, flags=re.I):
        # HTML template source is often indented around <br>. Treat that
        # indentation like the browser, preserving the explicit breaks only.
        text = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", text)
        text = re.sub(separator, "\n", text, flags=re.I)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    text = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))
    return re.sub(r"\[([^\]\r\n]{1,500})\]\([&A-Za-z0-9_][^)\r\n]{0,190}\)", r"\1", text).strip()


def _term(value):
    return " ".join(TOKENS.findall(value.replace("’", "'").lower()))


class Library:
    def __init__(self, service, profile):
        self.service = service
        self.game_id = profile["id"]
        self.title = profile["title"]
        self.pairs = profile["pairs"]
        self.reading = {}
        self.notebook = Notebook(service.data_dir)

    def properties(self, archive, schema):
        pairs, length, truncated = [], 0, False
        with self.service._db() as db:
            paired = "FROM entries WHERE game_id=? AND en<>'' AND zh<>''"
            count = db.execute("SELECT count(*) " + paired, (self.game_id,)).fetchone()[0]
            if not count:
                raise ValueError("双语语料库里没有这款游戏的配对文本，请先接入。")
            for row in db.execute("SELECT en,zh " + paired, (self.game_id,)):
                left, right = _text(row["en"]), _text(row["zh"])
                if left and right and left != right:
                    length += len(left) + len(right)
                    if length > 500_000:
                        # A subset could hide an otherwise ambiguous match.
                        pairs, truncated = [], True
                        break
                    pairs.append([left, right])
        labels, reading = {}, {}
        if schema:
            try:
                reading = schema.reading_data(archive, self.pairs, _text)
                labels = reading.pop("labels")
            except (KeyError, ValueError, OSError, RecursionError):
                labels = {key: [_text(left), _text(right)] for key, (left, right) in schema.ui_labels(archive).items()}
        archive.check_unchanged()
        self.reading = reading
        return dict(pairs=pairs, labels=labels, reading=reading, inline=bool(pairs or labels or reading), inlineLimited=truncated,
                    selectors=schema.TEXT_ELEMENTS if schema else "", count=count,
                    theme=native_overlay.theme(self.service, self.game_id), title=self.title,
                    categories=self.service.categories(self.game_id), presets=themes.presets(),
                    opacityRange=[round(themes.MIN_OPACITY * 100), round(themes.MAX_OPACITY * 100)],
                    cardSide=native_overlay.preferences(self.service, self.game_id).get("card_side", "right"))

    def _shown(self, row, language):
        value = row[language]
        entities = self.reading.get("entities")
        if entities and row["namespace"] == "scenes":
            from .engines.electron_red_pearls_reading import readable_scene
            value = readable_scene(value, entities, 0 if language == "en" else 1)
        return _text(value)

    def _entry(self, entry_id):
        if not isinstance(entry_id, str) or len(entry_id) > 256:
            raise ValueError("请选择一条游戏文本。")
        with self.service._db() as db:
            row = db.execute("SELECT * FROM entries WHERE id=? AND game_id=?", (entry_id, self.game_id)).fetchone()
        if row is None:
            raise ValueError("这条文本已更新，请重新检索。")
        return dict(row)

    def handle(self, request):
        action = request.get("action")
        if action == "appearance":
            return electron_panel.save_appearance(self.service, self.game_id, request.get("theme"), request.get("cardSide"))
        if action == "search":
            query = str(request.get("query", "")).strip()[:240]
            category = str(request.get("category", "")).strip()[:160]
            offset = request.get("offset", 0)
            page_size = 35
            if not query and not category:
                return dict(total=0, items=[], pageSize=page_size)
            result = self.service.search(query, self.game_id, category, limit=page_size,
                                         offset=min(max(0, offset), 1_000_000) if type(offset) is int else 0)
            result["pageSize"] = page_size
            def preview(row, language):
                text = self._shown(row, language)
                return text[:8192] + "\n[…]" if len(text) > 8192 else text
            result["items"] = [dict(id=row["id"], category=row["category"], speaker=row["speaker"],
                                    en=preview(row, "en"), zh=preview(row, "zh")) for row in result["items"]]
            return result
        if action not in {"lookup", "note"}:
            raise ValueError("面板请求无法识别。")
        row = self._entry(request.get("entry"))
        original = self._shown(row, "en")
        target = str(request.get("term", "")).strip()[:240]
        target_key = _term(target)
        if target:
            if not target_key or f" {target_key} " not in f" {_term(original)} ":
                raise ValueError("请从这句话中选择连续的英文单词。")
            context = json.loads(row["phrases"])
            match = next((item for item in context if _term(item.get("term", "")) == target_key), None)
            meaning = match or self.service.lookup(target_key, self.game_id)[0]
        else:
            meaning = {}
        if action == "lookup":
            return meaning
        kind = ("phrase" if " " in target_key else "word") if target else "sentence"
        snapshot = dict(row, en=original, zh=self._shown(row, "zh"))
        created = self.notebook.capture(snapshot, self.title, kind, target or original,
                                        target_key, meaning.get("meaning", ""),
                                        meaning.get("source", "原句注释" if meaning else ""))
        return dict(saved=created)


class GamePage:
    def __init__(self, pipe, archive, pages, schema, library):
        self.pipe = pipe
        self.archive = archive
        self.pages = pages
        self.schema = schema
        self.library = library
        self.properties = library.properties(archive, schema)
        self.source = (electron_panel.ASSETS / "panel.js").read_text(encoding="utf-8").strip()
        self.inline = (electron_panel.ASSETS / "inline.js").read_text(encoding="utf-8").strip()
        self.css = (electron_panel.ASSETS / "panel.css").read_text(encoding="utf-8")
        self.inline_css = (electron_panel.ASSETS / "inline.css").read_text(encoding="utf-8")
        self.session = None
        self.context = None

    def call(self, method, params=None):
        return self.pipe.call(method, params, session=self.session)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", dict(expression=expression, contextId=self.context, returnByValue=True))
        if "exceptionDetails" in result:
            error = result["exceptionDetails"]
            raise ProtocolError(error.get("exception", {}).get("description", error.get("text", "面板初始化失败。"))[:380])
        return result.get("result", {}).get("value")

    def attach(self):
        self.detach()
        targets = self.pipe.call("Target.getTargets")["targetInfos"]
        games = [target for target in targets if target.get("type") == "page"
                 and electron_panel.page_name(target.get("url", ""), self.archive.path, self.pages)]
        if len(games) != 1:
            return None
        target = games[0]
        self.session = self.pipe.call("Target.attachToTarget", dict(targetId=target["targetId"], flatten=True))["sessionId"]
        self.call("Runtime.enable")
        self.call("Runtime.addBinding", dict(name=BINDING, executionContextName=WORLD))
        frame = self.call("Page.getFrameTree")["frameTree"]["frame"]
        if not electron_panel.page_name(frame.get("url", ""), self.archive.path, self.pages):
            return None
        self.context = self.call("Page.createIsolatedWorld", dict(frameId=frame["id"], worldName=WORLD))["executionContextId"]
        options = dict(self.properties, path=urlsplit(frame["url"]).path, css=self.css, inlineCss=self.inline_css,
                       borneo=self.schema is not None)
        return self.evaluate(f"({self.source})({json.dumps(options, separators=(',', ':'))},({self.inline}))")

    def detach(self):
        if self.session:
            with suppress(ProtocolError, TimeoutError):
                self.pipe.call("Target.detachFromTarget", dict(sessionId=self.session))
        self.session, self.context = None, None

    def status(self):
        return self.evaluate("globalThis.bilingualWorkbench?.status()") if self.context is not None else None

    def handle_event(self, event):
        if not event or event.get("sessionId") != self.session:
            return
        args = event["params"]
        payload = args.get("payload", "")
        if args.get("name") != BINDING or args.get("executionContextId") != self.context or len(payload) > 4096:
            return
        try:
            request = json.loads(payload)
        except ValueError:
            return
        if not isinstance(request, dict) or type(request.get("id")) is not int:
            return
        try:
            value = self.library.handle(request)
            if request["action"] == "appearance":
                self.properties.update(value)
            result = dict(value=value)
        except Exception as exc:
            result = dict(error=str(exc)[:240])
        arguments = json.dumps([request["id"], result], separators=(",", ":"))
        self.evaluate(f"globalThis.bilingualWorkbench?.reply(...{arguments})")


def run(service, game_id, request):
    folder = electron_panel._folder(service, game_id)
    with electron_panel.launch_lock(folder):
        identity = {}
        channel = None
        started = False

        def report(state, detail, **extra):
            save_json(folder / "runtime.json", dict(request=request, state=state, detail=detail[:600],
                      updated=time.time(), **identity, **extra))

        try:
            report("starting", "正在核对 Electron 游戏包。")
            profile = service._profiles().load(game_id)
            if profile["engine"] != "electron" or not electron_panel.enabled(service, game_id):
                raise ValueError("请先在工作台启用这款游戏的 Electron 面板。")
            executable, archive, pages, schema = electron_panel.installation(profile)
            session = GameSession(executable)
            if session.is_running():
                raise ValueError("游戏已在运行，请先退出，再从工作台启动双语会话。")
            channel = ElectronPipe(executable, profile["root"])
            current = session.process_identity(channel.process.pid)
            if not current:
                raise ValueError("未能确认 Electron 游戏进程。")
            identity.update(pid=current["pid"], process_start_filetime=current["process_start_filetime"])
            report("starting", "已启动游戏，正在等待实际游戏页面。")
            page = GamePage(channel, archive, pages, schema, Library(service, profile))
            deadline = time.monotonic() + 25
            last_report = 0
            latest_error = ""
            while channel.process.poll() is None:
                now = time.monotonic()
                health = None
                try:
                    health = page.status() if now - last_report >= 2 else None
                    if now - last_report >= 2 and not health:
                        health = page.attach()
                    if health:
                        started = True
                        last_report = now
                        deadline = now + 25
                        report("ready", f"游戏页面与 {page.properties['count']:,} 条语料已连接。",
                               panel_drawn=bool(health["drawn"]), inline_count=health["annotations"])
                    page.handle_event(channel.next_event(0.18))
                except (ProtocolError, TimeoutError) as exc:
                    latest_error = str(exc)
                    page.context = None
                if time.monotonic() > deadline:
                    detail = "未能连接唯一可支持的 Electron 游戏页面。"
                    raise ValueError(detail + (" " + latest_error if latest_error else ""))
                if page.context is None and not health:
                    time.sleep(0.17)
            if channel.process.returncode != 0 or not started:
                raise ValueError(f"游戏在面板会话中退出（退出代码 {channel.process.returncode}）。")
        except Exception as exc:
            if isinstance(exc, PipeClosed) and channel:
                with suppress(TimeoutExpired):
                    channel.process.wait(timeout=3)
            code = channel.process.poll() if channel else None
            if not started or code != 0:
                report("failed", str(exc))
                # Electron owns the visible game window. Keep the inherited
                # pipe alive even if integration fails, until the user exits.
                if channel:
                    code = channel.process.wait()
                if started and code == 0 and isinstance(exc, PipeClosed):
                    report("exited", "游戏已退出。下次从工作台启动时会重新连接。")
                return
        finally:
            if channel:
                channel.close()
        report("exited", "游戏已退出。下次从工作台启动时会重新连接。")


if __name__ == "__main__":
    data_dir, game_id, request = sys.argv[1:]
    run(Service(data_dir=data_dir), game_id, request)
