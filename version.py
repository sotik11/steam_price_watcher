"""Application name + version — single source of truth.

Version scheme is 4-part **X.Y.Z.W**:
  * X — major update (breaking / big rework)
  * Y — new feature (minor update)
  * Z — large fix
  * W — small fix / hotfix

Bump exactly one part per release and reset the lower parts to 0.
The About tab and (when synced) the installer read this value.
"""

APP_NAME = "Steam Price Watcher"
__version__ = "0.1.3.2"

# Author / contact shown on the About tab.
APP_AUTHOR = "sotik + claude"
APP_CONTACT = "sotik11@gmail.com"
