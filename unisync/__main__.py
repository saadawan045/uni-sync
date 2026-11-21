import os
import sys

# Message text includes Urdu and emoji; Windows would otherwise read and write files as cp1252.
if not sys.flags.utf8_mode:
    args = [sys.executable, "-P", "-X", "utf8", "-m", "unisync", *sys.argv[1:]]
    if os.name == "nt":  # execv on Windows returns to the shell early, which breaks interactive setup
        import subprocess
        try:
            sys.exit(subprocess.call(args))
        except KeyboardInterrupt:
            sys.exit(130)
    os.execv(sys.executable, args)

from .cli import main  # noqa: E402

main()
