import asyncio
import logging
import tempfile
import time
from pathlib import Path

from telegram import BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .config import Settings
from .conversion import ConversionError
from .pdf_tools import selection_groups
from .sessions import SessionError, SessionStore
from .worker import WorkerRunner

LOGGER = logging.getLogger(__name__)
COMMANDS = [
    BotCommand("start", "Начать работу"),
    BotCommand("help", "Описание и инструкция"),
    BotCommand("compile", "Закончить добавление файлов и создать PDF"),
    BotCommand("cancel", "Отменить текущую операцию"),
    BotCommand("newpdf", "Начать новый PDF"),
    BotCommand("pdf", "Извлечь, удалить страницы или разделить PDF"),
]
KEYBOARD = ReplyKeyboardMarkup(
    [["Новый PDF", "Собрать PDF"], ["Действия с PDF"], ["Отмена", "Помощь"]], resize_keyboard=True
)
PDF_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Извлечь страницы", "Разделить на части"],
        ["Удалить страницы", "Добавить в сборку"],
        ["Отмена", "Помощь"],
    ],
    resize_keyboard=True,
)
PAGE_KEYBOARD = ReplyKeyboardMarkup(
    [["Действия с PDF", "Отмена"], ["Помощь"]],
    resize_keyboard=True,
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
            "Для отдельного PDF выберите действие в меню: извлечь страницы, удалить "
            "страницы или разделить на части. Вернуться к последнему PDF: /pdf.\n"
            "Номера страниц считаются с 1. Пример выборки: 1, 3-5, 12. "
            "Для разделения: по 5 или 1-5; 6-10. Части придут в ZIP.\n\n"
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
            "/cancel — очистить очередь\n/pdf — действия с последним PDF\n/help — инструкция\n\n"
            f"Неактивная очередь хранится {self.settings.ttl_seconds // 60} мин. "
            "После отправки общей сборки, отмены или перезапуска бота исходники удаляются. "
            "После операций со страницами исходный PDF остаётся для повторного использования. "
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
            session.pdf_action = None
            if path.suffix == ".pdf":
                session.pdf_target = path
                session.pdf_page_count = 0
        except SessionError as error:
            await message.reply_text(str(error))
            return
        except Exception as error:
            if path:
                path.unlink(missing_ok=True)
            LOGGER.warning("Ошибка загрузки: %s", type(error).__name__)
            await message.reply_text("Не удалось скачать файл. Попробуйте отправить его ещё раз.")
            return
        if path.suffix == ".pdf":
            await self.pdf_menu(update, context)
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
        self.launch(coroutine, update, context)

    def launch(self, coroutine, update, context):
        task = (
            context.application.create_task(coroutine, update=update)
            if context
            else asyncio.create_task(coroutine)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def pdf_session(self, update):
        if not await self.allowed(update):
            return None
        session = self.store.sessions.get(self.key(update))
        if session is None or session.pdf_target is None:
            await update.effective_message.reply_text(
                "Пришлите PDF, чтобы работать с его страницами."
            )
            return None
        if session.busy:
            await update.effective_message.reply_text(
                "Документ уже обрабатывается. Дождитесь результата или /cancel."
            )
            return None
        session.touched = time.monotonic()
        return session

    async def pdf_menu(self, update, context):
        session = await self.pdf_session(update)
        if session is None:
            return
        session.pdf_action = None
        await update.effective_message.reply_text(
            "PDF находится в очереди общей сборки. Выберите действие с последним присланным PDF:\n"
            "• Извлечь одну или несколько страниц.\n"
            "• Разделить документ на части и получить ZIP.\n"
            "• Удалить ненужные страницы из копии.\n"
            "• Продолжить общую сборку.\n\n"
            f"В очереди файлов: {len(session.files)}. Исходник при работе со страницами сохраняется.",
            reply_markup=PDF_KEYBOARD,
        )

    async def choose_pdf(self, update, context, action):
        session = await self.pdf_session(update)
        if session is None:
            return
        session.pdf_action = None
        if action == "merge":
            await update.effective_message.reply_text(
                "PDF уже добавлен в сборку. Пришлите ещё файлы или нажмите /compile.",
                reply_markup=KEYBOARD,
            )
            return
        session.busy = True
        try:
            await update.effective_message.reply_text("Проверяю PDF и число страниц…")
        except Exception:
            session.busy = False
            raise
        self.launch(
            self.pdf_job(update.effective_message, self.key(update), session, action),
            update,
            context,
        )

    async def pdf_input(self, update, context, session):
        if not await self.allowed(update):
            return
        if session.busy:
            await update.effective_message.reply_text(
                "Документ уже обрабатывается. Пожалуйста, подождите."
            )
            return
        session.touched = time.monotonic()
        selection = update.effective_message.text
        try:
            selection_groups(session.pdf_action, selection, session.pdf_page_count)
        except ConversionError as error:
            await update.effective_message.reply_text(
                str(error) + "\nПопробуйте ещё раз или откройте /pdf."
            )
            return
        session.busy = True
        try:
            await update.effective_message.reply_text("Обрабатываю выбранные страницы…")
        except Exception:
            session.busy = False
            raise
        self.launch(
            self.pdf_job(
                update.effective_message, self.key(update), session, session.pdf_action, selection
            ),
            update,
            context,
        )

    async def pdf_job(self, message, key, session, action, selection=None):
        filenames = {"extract": "pages.pdf", "remove": "remaining.pdf", "split": "parts.zip"}
        output = session.directory / filenames[action]
        try:
            if selection is None:
                count = await self.runner.inspect_pdf(session.pdf_target)
                if session.cancelled:
                    return
                session.pdf_page_count = count
                session.pdf_action = action
                prompt = {
                    "extract": "Какие страницы извлечь? Например: 1, 3-5. Порядок ввода сохраняется.",
                    "remove": "Какие страницы удалить из копии? Например: 1, 3-5. Нельзя удалить все страницы.",
                    "split": "Как разделить PDF? Напишите «по 5» для частей по пять страниц "
                    "или задайте части через точку с запятой: 1-5; 6-10. "
                    "Укажите все страницы ровно один раз. Результат придёт одним ZIP.",
                }[action]
                await message.reply_text(
                    f"В PDF страниц: {count}. Номера от 1 до {count} — позиции страниц в файле, "
                    f"а не напечатанные на листах номера.\n\n{prompt}",
                    reply_markup=PAGE_KEYBOARD,
                )
            else:
                await self.runner.edit_pdf(session.pdf_target, output, action, selection)
                if session.cancelled:
                    return
                session.sending = True
                captions = {
                    "extract": "Выбранные страницы готовы.",
                    "remove": "Копия без выбранных страниц готова.",
                    "split": "Готово! Части PDF находятся в ZIP-архиве.",
                }
                with output.open("rb") as stream:
                    await message.reply_document(
                        document=stream,
                        filename=output.name,
                        caption=captions[action]
                        + " Исходный PDF сохранён. Можно выбрать другое действие или /cancel.",
                        read_timeout=120,
                        write_timeout=120,
                        reply_markup=PDF_KEYBOARD,
                    )
                session.pdf_action = None
        except Exception as error:
            LOGGER.warning("Ошибка операции с PDF: %s", type(error).__name__)
            if not session.cancelled:
                text = (
                    str(error)
                    if isinstance(error, ConversionError)
                    else "Не удалось обработать или отправить PDF."
                )
                try:
                    await message.reply_text(
                        text + "\nИсходник сохранён. Повторите ввод или выберите действие: /pdf."
                    )
                except Exception:
                    LOGGER.warning("Не удалось отправить уведомление об ошибке PDF.")
        finally:
            output.unlink(missing_ok=True)
            session.busy = session.sending = False
            session.touched = time.monotonic()
            if session.cancelled:
                self.store.reset(key)
                try:
                    await message.reply_text(
                        "Очистка завершена. Можно присылать новые файлы.", reply_markup=KEYBOARD
                    )
                except Exception:
                    LOGGER.warning("Не удалось отправить уведомление об очистке PDF.")

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
        if not await self.allowed(update):
            return
        actions = {
            "Новый PDF": self.newpdf,
            "Собрать PDF": self.compile,
            "Отмена": self.cancel,
            "Помощь": self.help,
            "Действия с PDF": self.pdf_menu,
        }
        text = update.effective_message.text
        if text in actions:
            await actions[text](update, context)
            return
        pdf_actions = {
            "Извлечь страницы": "extract",
            "Удалить страницы": "remove",
            "Разделить на части": "split",
            "Добавить в сборку": "merge",
        }
        if text in pdf_actions:
            await self.choose_pdf(update, context, pdf_actions[text])
            return
        session = self.store.sessions.get(self.key(update))
        if session and session.pdf_action:
            await self.pdf_input(update, context, session)
        else:
            await self.help(update, context)


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
            "Также извлекаю и удаляю страницы, разделяю PDF на части. "
            "Пришлите файлы и выберите действие. Инструкция: /help."
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
        ("pdf", controller.pdf_menu),
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
