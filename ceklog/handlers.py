import asyncio
import html
import logging
import re
import socket
from io import BytesIO

import paramiko
from telegram import Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ceklog.config import BotConfig
from ceklog.ssh import fetch_clickhouse_errors, parse_time_range

logger = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
CODE_BLOCK_PATTERN = re.compile(r"```(.*?)```", re.S)
MENU_TEXT = (
    "Menu command:\n"
    "/cek61 HH:MM-HH:MM - Analisis log ClickHouse dan journal systemd "
    "pada rentang waktu hari ini (waktu server), contoh: /cek61 01:00-01:15"
)


class BotHandlers:
    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._ssh_lock = asyncio.Lock()

    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(MENU_TEXT)

    async def cek61(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return

        if len(_context.args) != 1:
            await message.reply_text(
                "Format: /cek61 HH:MM-HH:MM\n"
                "Contoh: /cek61 01:00-01:15 (hari ini, waktu server)"
            )
            return

        try:
            start_time, end_time = parse_time_range(_context.args[0])
        except ValueError as exc:
            await message.reply_text(str(exc))
            return

        await message.reply_text("Mengambil log dari server...")
        try:
            async with self._ssh_lock:
                result = await asyncio.to_thread(
                    fetch_clickhouse_errors,
                    self._config.server,
                    self._config.username,
                    self._config.password,
                    start_time,
                    end_time,
                    self._config.db_host,
                    self._config.db_scheme,
                    self._config.db_port,
                    self._config.db_username,
                    self._config.db_password,
                )
        except paramiko.AuthenticationException:
            logger.exception("Autentikasi SSH gagal")
            await self._send_code_block(
                update, "Gagal autentikasi SSH. Periksa username dan password."
            )
        except paramiko.BadHostKeyException:
            logger.exception("Host key SSH tidak cocok")
            await self._send_code_block(
                update, "Host key server SSH tidak cocok dengan known_hosts."
            )
        except (paramiko.SSHException, socket.timeout, OSError) as exc:
            logger.exception("Koneksi atau perintah SSH gagal")
            await self._send_code_block(update, f"Koneksi SSH gagal: {exc}")
        except RuntimeError as exc:
            logger.error("Perintah di server gagal: %s", exc)
            await self._send_code_block(update, f"Perintah di server gagal:\n{exc}")
        else:
            await self._send_result(message, result.summary)
            if result.query_evidence_workbook is not None:
                await message.reply_document(
                    document=BytesIO(result.query_evidence_workbook),
                    filename=result.query_evidence_filename
                    or "clickhouse_query_evidence.xlsx",
                    caption=(
                        "Evidence ClickHouse Excel: sheet query_views_log "
                        "dan query_log."
                    ),
                )

    async def _send_result(self, message: Message, result: str) -> None:
        if _utf16_length(result) > MAX_TELEGRAM_MESSAGE_LENGTH:
            if "Bukti dari log:" in result:
                summary = result.split("Bukti dari log:", maxsplit=1)[0]
                text = (
                    f"{summary.rstrip()}\n"
                    "Hasil analisis terlalu panjang, log lengkap terlampir.\n"
                    "Bukti dari log:\n\n"
                )
            else:
                text = (
                    "Hasil analisis terlalu panjang. "
                    "Ringkasan lengkap terlampir sebagai ch_restart_log.txt."
                )
            if _utf16_length(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
                text = (
                    "Hasil analisis terlalu panjang. "
                    "Ringkasan lengkap terlampir sebagai ch_restart_log.txt."
                )
            await message.reply_text(text)
            await message.reply_document(
                document=BytesIO(result.encode("utf-8")),
                filename="ch_restart_log.txt",
            )
            return

        await message.reply_text(
            _format_code_blocks_html(result),
            parse_mode="HTML",
        )

    @staticmethod
    async def _send_code_block(update: Update, text: str) -> None:
        if update.effective_message is None:
            return
        escaped = html.escape(text)
        await update.effective_message.reply_text(
            f"<pre>{escaped}</pre>", parse_mode="HTML"
        )


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _format_code_blocks_html(text: str) -> str:
    parts: list[str] = []
    position = 0
    for match in CODE_BLOCK_PATTERN.finditer(text):
        parts.append(html.escape(text[position : match.start()]))
        parts.append(f"<pre>{html.escape(match.group(1))}</pre>")
        position = match.end()
    parts.append(html.escape(text[position:]))
    return "".join(parts)


def register_handlers(application: Application, config: BotConfig) -> None:
    handlers = BotHandlers(config)
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("cek61", handlers.cek61))
