"""Launch the desktop workbench, or rebuild/inspect its local index."""
import argparse
import json
import logging
import sys
from pathlib import Path

from bilingual.core import Service


def main():
    parser = argparse.ArgumentParser(description="双语游戏工作台")
    parser.add_argument("--refresh", action="store_true", help="更新本机语料索引后退出")
    parser.add_argument("--status", action="store_true", help="输出已接入游戏的状态")
    parser.add_argument("--launch", metavar="GAME_ID", help="启动指定双语游戏并连接检索面板")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    (project / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=project / "logs/workbench.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    service = Service(project)
    if args.refresh:
        print(json.dumps(service.refresh(), ensure_ascii=False, indent=2))
    elif args.status:
        print(json.dumps(service.games(), ensure_ascii=False, indent=2))
    else:
        # Every launcher uses the same workbench instance. Native games keep a
        # minimized taskbar entry so their automatic runtime checks stay visible.
        from bilingual.instance import Instance
        from bilingual.ui import run
        instance = Instance(service.data_dir)
        if not instance.claim():
            try:
                instance.send(args.launch)
            except Exception as exc:
                import tkinter.messagebox
                tkinter.messagebox.showerror("工作台已在运行", str(exc))
            return
        def ready(app):
            def receive(message):
                if message["type"] == "launch":
                    app.request_launch(message["game_id"])
                else:
                    def show():
                        app.return_session = None
                        app.root.deiconify()
                        app.root.lift()
                        app.search_box.focus_set()
                    app.events.put((show, (), False))
            instance.start(receive)
        try:
            run(service, launch_game=args.launch, on_ready=ready)
        finally:
            instance.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("启动失败")
        if sys.stderr:
            raise
        import tkinter.messagebox
        tkinter.messagebox.showerror("启动失败", "请查看 logs/workbench.log；可用启动诊断.cmd查看错误。")
