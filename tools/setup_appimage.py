import sys

from pi_media_hub.gui import main as gui_main
from pi_media_hub.setup import main as setup_main


def main():
    if sys.argv[1:2] == ["install"]:
        raise SystemExit(setup_main())
    if sys.argv[1:2] == ["--install-help"]:
        raise SystemExit(setup_main(["--help"]))
    if sys.argv[1:2] == ["--gui-smoke-test"]:
        import tkinter as tk
        from pi_media_hub.gui import MediaHubApp
        root = tk.Tk()
        root.withdraw()
        MediaHubApp(root)
        root.update_idletasks()
        root.destroy()
        return
    gui_main()


if __name__ == "__main__":
    main()
