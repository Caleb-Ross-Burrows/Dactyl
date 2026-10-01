"""
Runs the default simulation, or one described by a settings file.

    python simulationEngine/pyFemmSim.py                 # default settings (see SimConfig.DEFAULT_CONFIG)
    python simulationEngine/pyFemmSim.py settings.json   # your own settings

Coils and the payload are no longer edited in this file: change them in the web viewer's
"New simulation" panel (python webViewer/server.py) or in a settings file.
"""

import sys

from run_config import main

if __name__ == "__main__":
    sys.exit(main())
