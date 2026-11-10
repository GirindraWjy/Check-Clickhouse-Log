import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    server: str
    username: str
    password: str
    telegram_token: str
    db_host: str = ""
    db_scheme: str = ""
    db_port: str = ""
    db_username: str = ""
    db_password: str = ""


def load_config() -> BotConfig:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path, override=True)

    values = {
        "server": os.getenv("server", ""),
        "username": os.getenv("username", ""),
        "password": os.getenv("password", ""),
        "token": os.getenv("token", ""),
        "db_host": os.getenv("db_host", ""),
        "db_scheme": os.getenv("db_scheme", ""),
        "db_port": os.getenv("db_port", ""),
        "db_username": os.getenv("db_usn", ""),
        "db_password": os.getenv("db_pass", ""),
    }
    required = ("server", "username", "password", "token")
    missing = [name for name in required if not values[name].strip()]
    if missing:
        raise RuntimeError(
            f"Variabel berikut belum diisi di .env: {', '.join(missing)}"
        )

    return BotConfig(
        server=values["server"].strip(),
        username=values["username"].strip(),
        password=values["password"],
        telegram_token=values["token"].strip(),
        db_host=values["db_host"].strip(),
        db_scheme=values["db_scheme"].strip(),
        db_port=values["db_port"].strip(),
        db_username=values["db_username"].strip(),
        db_password=values["db_password"],
    )
