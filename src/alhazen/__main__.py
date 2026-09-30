"""``python -m alhazen …``: the same command line as the ``alhazen`` script.

Why a second way to start the same thing: on Windows, the ``alhazen`` command
is ``alhazen.exe``, a small launcher pip writes into the environment's
``Scripts`` folder, and a running program's file is locked there. Reinstalling
alhazen into that environment (``pip install -e .`` after a release) must
replace the launcher, fails part-way on the lock, and can leave alhazen
uninstalled from the environment — while the dashboard that holds the lock keeps
running on the code it started with, so nothing looks wrong until the next
command. Started as ``python -m alhazen dashboard``, the process holds only
``python.exe``, which a reinstall never touches.

Everything else is identical: the arguments go to ``alhazen.cli.main.main``,
and its return value is the exit status, exactly as the launcher does it.
"""

from alhazen.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
