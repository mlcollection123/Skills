"""Launcher used for the packaged .exe: starts the UI and records any crash to a log file."""
import os
import sys
import traceback

if sys.stdout is None:   # windowed .exe has no console
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def _report_crash(text: str) -> None:
    where = ""
    try:
        from file_organizer.config import app_dir
        where = os.path.join(app_dir(), "crash.log")
        with open(where, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("File Organizer crashed",
                             f"{text[-1500:]}\n\nDetails saved to:\n{where or '(could not write log)'}")
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        from file_organizer.__main__ import main
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        _report_crash(traceback.format_exc())
        sys.exit(1)
