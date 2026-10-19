import asyncio
import logging
import tempfile
import time
from pathlib import Path

from telegram import BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .config import Settings
from .conversion import ConversionError
from .sessions import SessionError, SessionStore
from .worker import WorkerRunner

LOGGER = logging.getLogger(__name__)
COMMANDS = [
    BotCommand("start", "Начать работу"),
    BotCommand("help", "Описание и инструкция"),
    BotCommand("compile", "Закончить добавление файлов и создать PDF"),
    BotCommand("cancel", "Отменить текущую операцию"),
    BotCommand("newpdf", "Начать новый PDF"),
]
KEYBOARD = ReplyKeyboardMarkup(
    [["Новый PDF", "Собрать PDF"], ["Отмена", "Помощь"]], resize_keyboard=True
)


class Controller:
    def __init__(self, settings, store, runner):
        self.settings = settings
        self.store = store
        self.runner = runner
        self.tasks: set[asyncio.Task] = set()

    async def allowed(self, update):
        if not update.effective_message or not update.effective_user or not update.effective_chat:
            return False
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Пришлите файлы в личный чат с ботом.")
            return False
        if (
            self.settings.allowed_user_ids
            and update.effective_user.id not in self.settings.allowed_user_ids
        ):
            await update.effective_message.reply_text("Доступ к этому боту ограничен владельцем.")
            return False
        return True

    @staticmethod
    def key(update):
        return update.effective_chat.id, update.effective_user.id

    async def help(self, update, context):
        if not await self.allowed(update):
            return
        await update.effective_message.reply_text(
            "📄 Соберу ваши файлы в один PDF.\n\n"
            "1. Пришлите фото или документы — можно вперемешку и альбомами.\n"
            "2. Дождитесь подтверждения добавления всех файлов.\n"
            "3. Нажмите «Собрать PDF» или /compile.\n\n"
            "Порядок страниц соответствует порядку сообщений. Для лучшего качества "
            "отправляйте фотографии как файлы.\n\n"
            "Форматы: JPG, PNG, WEBP, BMP, TIFF, GIF, HEIC/HEIF, PDF; "
            "TXT, CSV, MD (как текст); DOC/DOCX, ODT, RTF, XLS/XLSX, ODS, PPT/PPTX, ODP. "
            "Офисные форматы требуют LibreOffice на сервере.\n\n"
            f"До {self.settings.max_files} файлов, до {self.settings.max_file_bytes // 1_000_000} МБ "
            f"каждый, очередь до {self.settings.max_total_bytes // 1_000_000} МБ, "
            f"до {self.settings.max_pages} страниц. PDF до "
            f"{self.settings.max_output_bytes // 1_000_000} МБ.\n\n"
            "/newpdf — начать новый PDF\n/compile — собрать PDF\n"
            "/cancel — очистить очередь\n/help — инструкция\n\n"
            f"Неактивная очередь хранится {self.settings.ttl_seconds // 60} мин. "
            "После отправки PDF, отмены или перезапуска бота исходники удаляются. "
            "PDF с паролем не поддерживается. Интерактивные поля и аннотации PDF "
            "не переносятся; для их сохранения заранее экспортируйте плоский PDF.",
            reply_markup=KEYBOARD,
        )

    async def newpdf(self, update, context):
        if not await self.allowed(update):
            return
        key = self.key(update)
        session = self.store.sessions.get(key)
        if session and session.busy:
            await update.effective_message.reply_text(
                "Предыдущий PDF ещё обрабатывается. Нажмите /cancel и дождитесь очистки."
            )
            return
        self.store.reset(key)
        await update.effective_message.reply_text(
            "Новая очередь готова. Пришлите файлы, затем нажмите «Собрать PDF».",
            reply_markup=KEYBOARD,
        )

    async def cancel(self, update, context):
        if not await self.allowed(update):
            return
        key = self.key(update)
        session = self.store.sessions.get(key)
        if session and session.sending:
            await update.effective_message.reply_text(
                "PDF уже отправляется в Telegram. Дождитесь завершения отправки."
            )
        elif session and session.busy:
            session.cancelled = True
            await update.effective_message.reply_text(
                "Сборка отменена, PDF не будет отправлен. Завершаю обработку и очищаю файлы."
            )
        else:
            self.store.reset(key)
            await update.effective_message.reply_text(
                "Очередь очищена. Можно присылать новые файлы.", reply_markup=KEYBOARD
            )

    async def receive(self, update, context):
        if not await self.allowed(update):
            return
        message = update.effective_message
        incoming = message.document or (message.photo[-1] if message.photo else None)
        if incoming is None:
            await message.reply_text("Пришлите фотографию или документ. Инструкция: /help.")
            return
        name = (incoming.file_name or "document") if message.document else "photo.jpg"
        path = None
        try:
            session = self.store.get(self.key(update))
            path = self.store.allocate(session, name, incoming.file_size)
            remote = await incoming.get_file()
            await remote.download_to_drive(custom_path=path)
            self.store.accept(session, path)
        except SessionError as error:
            await message.reply_text(str(error))
            return
        except Exception as error:
            if path:
                path.unlink(missing_ok=True)
            LOGGER.warning("Ошибка загрузки: %s", type(error).__name__)
            await message.reply_text("Не удалось скачать файл. Попробуйте отправить его ещё раз.")
            return
        await message.reply_text(
            f"Добавлено файлов: {len(session.files)}. "
            f"Общий размер: {session.total_bytes / 1_000_000:.1f} МБ.\n"
            "Пришлите ещё файлы или нажмите «Собрать PDF».",
            reply_markup=KEYBOARD,
        )

    async def compile(self, update, context):
        if not await self.allowed(update):
            return
        key = self.key(update)
        session = self.store.sessions.get(key)
        if session is None or not session.files:
            await update.effective_message.reply_text("Сначала пришлите хотя бы один файл.")
            return
        if session.busy:
            await update.effective_message.reply_text("PDF уже собирается. Пожалуйста, подождите.")
            return
        session.busy = True
        try:
            await update.effective_message.reply_text(
                "Собираю PDF. Это может занять несколько минут…"
            )
        except Exception:
            session.busy = False
            raise
        coroutine = self.build_and_send(update.effective_message, key, session)
        task = (
            context.application.create_task(coroutine, update=update)
            if context
            else asyncio.create_task(coroutine)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def build_and_send(self, message, key, session):
        output = session.directory / "document.pdf"
        delivered = False
        try:
            await self.runner(list(session.files), output)
            if session.cancelled:
                return
            session.sending = True
            with output.open("rb") as stream:
                await message.reply_document(
                    document=stream,
                    filename="document.pdf",
                    caption="Готово! Все файлы собраны в один PDF.",
                    read_timeout=120,
                    write_timeout=120,
                    reply_markup=KEYBOARD,
                )
            delivered = True
        except Exception as error:
            LOGGER.warning("Ошибка сборки или отправки: %s", type(error).__name__)
            if not session.cancelled:
                text = (
                    str(error)
                    if isinstance(error, ConversionError)
                    else "Не удалось собрать или отправить PDF. Попробуйте /compile ещё раз."
                )
                try:
                    await message.reply_text(text + "\nОчередь сохранена. Очистить: /cancel.")
                except Exception:
                    LOGGER.warning("Не удалось отправить уведомление об ошибке.")
        finally:
            output.unlink(missing_ok=True)
            session.busy = session.sending = False
            session.touched = time.monotonic()
            if delivered or session.cancelled:
                self.store.reset(key)
            if session.cancelled:
                try:
                    await message.reply_text("Очистка завершена. Можно присылать новые файлы.")
                except Exception:
                    LOGGER.warning("Не удалось отправить уведомление об очистке.")

    async def wait_tasks(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks))

    async def text(self, update, context):
        actions = {
            "Новый PDF": self.newpdf,
            "Собрать PDF": self.compile,
            "Отмена": self.cancel,
            "Помощь": self.help,
        }
        action = actions.get(update.effective_message.text, self.help)
        await action(update, context)


def make_application(settings, root):
    store = SessionStore(
        root,
        **{
            name: getattr(settings, name)
            for name in (
                "max_files",
                "max_file_bytes",
                "max_total_bytes",
                "max_sessions",
                "ttl_seconds",
            )
        },
    )
    controller = Controller(settings, store, WorkerRunner(settings))

    async def setup(application):
        await application.bot.set_my_commands(COMMANDS)
        await application.bot.set_my_commands(COMMANDS, language_code="ru")
        await application.bot.set_my_description(
            "Собираю фотографии, PDF, текст и офисные документы в один PDF. "
            "Пришлите файлы и нажмите /compile. Инструкция: /help."
        )
        await application.bot.set_my_short_description(
            "Фото и документы в один PDF. Всё на русском."
        )

    async def cleanup(context):
        store.expire()

    async def on_error(update, context):
        # Исключения HTTP могут содержать URL с токеном: не выводим их текст/traceback.
        LOGGER.error("Ошибка обработки обновления: %s", type(context.error).__name__)

    application = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(False)
        .post_init(setup)
        .build()
    )
    for command, action in (
        ("start", controller.help),
        ("help", controller.help),
        ("newpdf", controller.newpdf),
        ("compile", controller.compile),
        ("cancel", controller.cancel),
    ):
        application.add_handler(CommandHandler(command, action))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, controller.receive)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.text))
    application.add_handler(MessageHandler(filters.ALL, controller.help))
    application.add_error_handler(on_error)
    application.job_queue.run_repeating(cleanup, interval=60, first=60)
    return application


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # HTTP-библиотеки могут логировать URL с BOT_TOKEN.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError:
        raise SystemExit(
            "Ошибка настроек. Проверьте BOT_TOKEN и параметры в .env по .env.example."
        ) from None
    try:
        with tempfile.TemporaryDirectory(prefix="universe-pdf-") as root:
            application = make_application(settings, Path(root))
            application.run_polling(allowed_updates=["message"], drop_pending_updates=False)
    except Exception as error:
        LOGGER.error("Бот остановлен: %s. Проверьте настройки и соединение.", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
