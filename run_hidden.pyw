"""Double-click to launch without a console; safe for Windows worker imports."""
if __name__ == "__main__":
    import os
    from pathlib import Path
    from halo import main

    os.chdir(Path(__file__).resolve().parent)
    raise SystemExit(main())
