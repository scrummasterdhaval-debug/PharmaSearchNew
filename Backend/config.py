import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from a local .env into the environment.

    The admin sign-in is configured through three environment variables, and
    setting those in every shell that starts the server is easy to get wrong on
    the machine a demo runs on -- one missed variable and nobody gets past the
    login page. A .env beside this file lets them be written once.

    A variable already set in the real environment wins, so a deployment that
    configures its environment properly is never overridden by a stray file.
    """
    try:
        # Bytes, split on newlines only. Text mode and splitlines() both treat
        # a carriage return as a line end, and a value pasted through a web
        # console arrived as "KEY=<CR>value": it read as an empty KEY and a
        # stray line, silently.
        lines = path.read_bytes().decode("utf-8", errors="replace").split("\n")
    except OSError:
        return
    for line in lines:
        line = "".join(char for char in line if char >= " " or char == "\t").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

DB_PATH = Path(os.getenv("PHARMASEARCH_DB_PATH", BASE_DIR / "pharmasearch.db"))
EXPORT_DIR = Path(os.getenv("PHARMASEARCH_EXPORT_DIR", BASE_DIR / "exports"))

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
