import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database import AdmissionError
from bot.service import SubmissionService

logger = logging.getLogger(__name__)
WELCOME = (
    "<b>Привет! 👋</b>\n\n"
    "Это анонимная предложка канала «Подслушано РАНХиГС | Волгоград».\n\n"
    "Здесь можно поделиться историей, задать вопрос студентам, признаться в чём-нибудь "
    "или просто рассказать о студенческой жизни.\n\n"
    "Напиши сообщение, и оно появится в канале после автоматической проверки или модерации.\n\n"
    "🔒 Твоё имя и Telegram-профиль не публикуются. Администратору доступны текст и твой "
    "Telegram ID для модерации и блокировок. Подробнее: /privacy"
)
HELP_ADMIN = (
    "/pending [страница] — очередь, по 5 заявок\n"
    "/stats — статистика\n"
    "/ban USER_ID — блокировка и отклонение ожидающих заявок автора\n"
    "/unban USER_ID — снять блокировку\n"
    "/autopublish on или off — автопубликация новых заявок\n"
    "/resolve ID MESSAGE_ID — подтвердить, что пост с неизвестным результатом уже есть в канале\n"
    "/retry ID — после проверки отсутствия поста вернуть заявку с неизвестным результатом в очередь\n"
    "/purge_rejected — удалить отклонённые заявки старше срока хранения\n"
    "/help_admin — эта справка\n\n"
    "При неизвестном результате сначала проверьте канал: повторная отправка может создать дубликат."
)


def positive_integer(value: str) -> int:
    result = int(value)
    if result <= 0 or result > 2**63 - 1:
        raise ValueError
    return result


def create_router(service: SubmissionService) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message(CommandStart())
    async def start(message: Message):
        await message.answer(
            WELCOME,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✍️ Отправить публикацию", callback_data="submit")],
                ]
            ),
        )

    @router.callback_query(F.data == "submit")
    async def prompt(callback: CallbackQuery):
        await callback.answer()
        await callback.message.answer("Напиши историю одним текстовым сообщением 📝")

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        await message.answer(
            "🔒 Публикации анонимны для читателей канала. Имя, профиль и Telegram ID автора "
            "не добавляются к посту. Оригиналы сообщений не пересылаются.\n\n"
            "Для модерации бот хранит текст, Telegram ID, номер исходного сообщения, время, статус "
            "и причины проверки. Администратор имеет доступ к этим данным и может блокировать отправителей. "
            "Абсолютной анонимности перед владельцем бота нет. Не отправляй чужие личные данные.\n\n"
            f"Отклонённые заявки удаляются из рабочей базы через {service.config.rejected_retention_days} дн. "
            "Принятые заявки и блокировки хранятся до удаления владельцем. Копии в чате администратора "
            "и резервных копиях удаляет владелец отдельно. По вопросам удаления данных обратись к администрации канала.",
            parse_mode=None,
        )

    @router.message(
        Command("pending", "stats", "ban", "unban", "autopublish", "help_admin", "resolve", "retry", "purge_rejected")
    )
    async def admin_command(message: Message, command: CommandObject):
        if not message.from_user or message.from_user.id != service.config.admin_id:
            await message.answer("Команда доступна только администратору.")
            return
        args = (command.args or "").split()
        name = command.command.lower()
        try:
            if name == "help_admin":
                await message.answer(HELP_ADMIN)
            elif name == "pending":
                page = positive_integer(args[0]) if args else 1
                rows = await service.db.pending(page=page - 1)
                if not rows:
                    await message.answer("На этой странице заявок нет.")
                for row in rows:
                    await service.notify_admin(row["id"], force=True)
                if rows:
                    await message.answer(f"Страница {page}. Следующая: /pending {page + 1}")
            elif name == "stats":
                stats = await service.db.stats()
                enabled = await service.db.auto_publish()
                await message.answer(
                    f"Всего заявок в базе: {stats['total']}\nАвтоматически опубликовано: {stats['auto']}\n"
                    f"Опубликовано вручную: {stats['manual']}\nОтклонено: {stats['rejected']}\n"
                    f"Ожидают модерации: {stats['pending']}\nОтправляются / результат неизвестен: {stats['uncertain']}\n"
                    f"Заблокированных пользователей: {stats['banned']}\nЗа последние сутки: {stats['today']}\n"
                    f"Автопубликация: {'включена' if enabled else 'выключена'}\n"
                    "Удалённые по сроку хранения заявки в статистику не входят."
                )
            elif name in {"ban", "unban"}:
                if len(args) != 1:
                    raise ValueError
                user_id = positive_integer(args[0])
                await (service.db.ban(user_id) if name == "ban" else service.db.unban(user_id))
                await message.answer("Пользователь заблокирован." if name == "ban" else "Блокировка снята.")
            elif name == "autopublish":
                if args not in [["on"], ["off"]]:
                    raise ValueError
                await service.db.set_auto_publish(args[0] == "on")
                await message.answer(
                    "Автопубликация включена." if args[0] == "on" else "Все новые заявки направляются на модерацию."
                )
            elif name == "purge_rejected":
                count = await service.db.purge_rejected(service.config.rejected_retention_days)
                await message.answer(f"Удалено отклонённых заявок: {count}.")
            elif name == "resolve":
                if len(args) != 2:
                    raise ValueError
                submission_id, message_id = map(positive_integer, args)
                changed = await service.db.resolve(submission_id, message_id)
                if changed:
                    row = await service.db.get(submission_id)
                    await service.notify_user(row["user_id"], "✅ Твоя публикация появилась в канале!")
                await message.answer(
                    "Публикация подтверждена." if changed else "Заявка не найдена или её статус уже изменился."
                )
            elif name == "retry":
                if len(args) != 1:
                    raise ValueError
                submission_id = positive_integer(args[0])
                changed = await service.db.resolve(submission_id, None)
                await message.answer(
                    "Заявка возвращена в очередь. Нажмите «Опубликовать» после проверки канала."
                    if changed
                    else "Заявка не найдена или её статус уже изменился."
                )
                if changed:
                    await service.notify_admin(submission_id)
        except (ValueError, IndexError):
            await message.answer("Неверные аргументы. Формат команд: /help_admin")

    @router.callback_query(F.data.startswith("mod:"))
    async def moderate(callback: CallbackQuery):
        if callback.from_user.id != service.config.admin_id:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 3 or parts[1] not in {"publish", "reject", "ban"}:
                raise ValueError
            submission_id = positive_integer(parts[2])
        except ValueError:
            await callback.answer("Некорректная кнопка", show_alert=True)
            return
        await callback.answer("Обрабатываю заявку…")
        if parts[1] == "publish":
            result = await service.publish(submission_id)
            status = {
                "published": "✅ Опубликовано",
                "pending": "Не отправлено. Проверьте права бота и повторите позже.",
                "uncertain": "⚠️ Результат неизвестен. Проверьте канал и /pending.",
                "publishing": "Заявка уже отправляется. Повторная отправка не запущена.",
            }.get(result, "Заявка уже обработана или недоступна.")
        else:
            changed = await service.reject(submission_id, ban=parts[1] == "ban")
            status = (
                ("🚫 Автор заблокирован" if parts[1] == "ban" else "❌ Отклонено")
                if changed
                else "Заявка уже обработана или недоступна."
            )
        row = await service.db.get(submission_id)
        if row is None or row["status"] != "pending":
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.warning("Could not remove obsolete moderation buttons")
        await callback.message.answer(f"Заявка #{submission_id}: {status}")

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message):
        await message.answer("Отправь историю обычным текстом. Доступны /start и /privacy.")

    @router.message(F.text)
    async def submission(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        risky_entities = any(entity.type in {"text_link", "text_mention"} for entity in message.entities or [])
        try:
            _, response = await service.submit(message.from_user.id, message.text, message.message_id, risky_entities)
        except AdmissionError as error:
            response = str(error)
        await message.answer(response)

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Пока я принимаю только текстовые сообщения 📝")

    @router.errors()
    async def error_handler(event: ErrorEvent):
        # Never log Telegram updates, exception strings, texts, IDs or database URLs.
        logger.error("Update processing failed (%s)", type(event.exception).__name__)
        if event.update.message and event.update.message.chat.type == "private":
            try:
                await event.update.message.answer(
                    "Не удалось завершить обработку. Если заявка принята, она сохранена для проверки администратором."
                )
            except Exception:
                pass
        return True

    return router
