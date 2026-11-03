from .cli import main
from .logsetup import setup_logging

if __name__ == "__main__":
    setup_logging()
    raise SystemExit(main())
