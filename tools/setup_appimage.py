import sys

from pi_media_hub.gui import main as gui_main
from pi_media_hub.setup import main as setup_main


def main():
    if sys.argv[1:2] == ["install"]:
        raise SystemExit(setup_main())
    if sys.argv[1:2] == ["--install-help"]:
        raise SystemExit(setup_main(["--help"]))
    gui_main()


if __name__ == "__main__":
    main()
