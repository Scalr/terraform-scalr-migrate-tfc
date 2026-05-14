import sys

if sys.version_info < (3, 12):
    sys.exit("Python 3.12 or higher is required")

from scalr_tfc_migrate.cli import main

if __name__ == "__main__":
    main()
