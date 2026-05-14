"""Terminal logging helpers."""

class ConsoleOutput:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{cls.CYAN}[INFO]{cls.ENDC} {message}")

    @classmethod
    def success(cls, message: str) -> None:
        print(f"{cls.GREEN}[SUCCESS]{cls.ENDC} {message}")

    @classmethod
    def warning(cls, message: str) -> None:
        print(f"{cls.WARNING}[WARNING]{cls.ENDC} {message}")

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls.FAIL}[ERROR]{cls.ENDC} {message}")

    @classmethod
    def debug(cls, message: str) -> None:
        print(f"{cls.BLUE}[DEBUG]{cls.ENDC} {message}")

    @classmethod
    def section(cls, message: str) -> None:
        print(f"\n{cls.HEADER}{cls.BOLD}{message}{cls.ENDC}")
        print(f"{cls.HEADER}{'=' * len(message)}{cls.ENDC}\n")
