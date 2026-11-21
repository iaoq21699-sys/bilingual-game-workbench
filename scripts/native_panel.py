"""Install/remove the game-process overlay using owned-file transactions."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bilingual.core import Service
from bilingual import native_overlay


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "remove", "status", "inspect", "theme", "health"))
    parser.add_argument("game_id")
    parser.add_argument("--preset", choices=("warm", "cool", "neutral"))
    parser.add_argument("--image", help="从本地游戏菜单截图自动提色")
    parser.add_argument("--renderer", choices=("auto", "D3D10", "D3D11", "D3D12"), default="auto")
    args = parser.parse_args()
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    service = Service()
    if args.action == "health":
        from bilingual.native_health import NativeHealthMonitor
        print(json.dumps(NativeHealthMonitor(service, args.game_id).poll(), ensure_ascii=False, indent=2))
    elif args.action == "theme" and (args.preset or args.image):
        from bilingual import themes
        appearance = themes.theme_from_image(Path(args.image)) if args.image else themes.presets()[args.preset]
        print(native_overlay.save_preferences(service, args.game_id, appearance, args.renderer))
    else:
        result = native_overlay.inspect(service, args.game_id, args.renderer) if args.action == "inspect" else getattr(native_overlay, args.action)(service, args.game_id)
        print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result)


if __name__ == "__main__":
    main()
