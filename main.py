import logging

from telegram.ext import Application

from ceklog.config import load_config
from ceklog.handlers import register_handlers


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()
    application = Application.builder().token(config.telegram_token).build()
    register_handlers(application, config)
    application.run_polling()


if __name__ == "__main__":
    main()
