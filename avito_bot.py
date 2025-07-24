import configparser
import logging
import requests
import time
import os
import html
import json
import re
import asyncio
import pandas as pd
from openpyxl.utils import get_column_letter
import io
import sqlite3
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, ReplyKeyboardMarkup, \
    ReplyKeyboardRemove
from telegram.ext import (Application, CommandHandler, ConversationHandler,
                          MessageHandler, filters, ContextTypes, CallbackQueryHandler)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import database as db
import avito_api as avito

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = 'config.ini'
LAST_TIMESTAMPS_FILE = 'last_timestamps.json'
STATUS_FILE = 'bot_status.json'
AI_SETTINGS_FILE = 'ai_settings.json'
ITEMS_PER_PAGE = 5
CANCEL_KEYBOARD = ReplyKeyboardMarkup([['/cancel']], resize_keyboard=True, one_time_keyboard=True)

(
    MAIN_MENU, ACCOUNTS_MENU, EDIT_ACCOUNT_LIST, EDIT_ACCOUNT_MENU, DELETE_ACCOUNT_LIST,
    TEMPLATES_MENU, TEMPLATE_SELECT_CATEGORY,
    CATEGORIES_MENU, VIEW_CATEGORY_TEMPLATES,
    STATS_MENU, SHOW_STATS,
    AI_MENU, AI_PROMPTS_MENU, AI_KEYS_MENU,
    SEARCH_SELECT_ACCOUNT, SEARCH_SHOW_RESULTS,
    CHOOSE_PROVIDER_FOR_ACCOUNT,
    CHOOSE_AI_MODE, AWAITING_AI_DELAY, AWAITING_GLOBAL_AI_DELAY, CHOOSE_CATEGORY_FOR_ACCOUNT,
    CHOOSE_PROMPT_TYPE_FOR_ACCOUNT, CHOOSE_PROMPT_FOR_ACCOUNT,
    AI_MY_PROMPTS_LIST, AI_PROMPTS_EDIT_MENU, AWAITING_PROMPT_NEW_NAME, AWAITING_PROMPT_NEW_TEXT,
    AI_DELETE_PROMPT_LIST,
    CHOOSE_AUTOREPLY_TEMPLATE,
    TEMPLATES_MY_LIST, TEMPLATES_EDIT_MENU, AWAITING_TEMPLATE_NEW_TEXT, AWAITING_TEMPLATE_NEW_NAME,

    AWAITING_MANUAL_REPLY,
    ADD_ACCOUNT_NAME, ADD_ACCOUNT_CLIENT_ID, ADD_ACCOUNT_CLIENT_SECRET, ADD_ACCOUNT_PROFILE_ID, ADD_ACCOUNT_CHAT_ID,
    EDIT_ACCOUNT_FIELD,
    ADD_TEMPLATE_NAME, ADD_TEMPLATE_TEXT,
    ADD_CATEGORY_NAME,
    GET_API_KEY,
    SEARCH_AWAIT_QUERY,
    AI_ADD_PROMPT_NAME, AI_ADD_PROMPT_TEXT,

    ACCOUNT_DATA_MENU, AUTOMATION_SETTINGS_MENU, DELETE_ACCOUNT_CONFIRM,
    TEMPLATES_SHOW_CATEGORIES, TEMPLATES_SHOW_IN_CATEGORY, TEMPLATES_CATEGORY_SETTINGS, AWAITING_CATEGORY_RENAME

) = range(54)

DEFAULT_PROMPT = "Ты — менеджер по продажам."


def load_json(file_path, default_data=None):
    if default_data is None: default_data = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_data


def save_json(file_path, data):
    with open(file_path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)


def _build_chat_interaction_keyboard(account, chat_id_avito):
    keyboard = [
        [
            InlineKeyboardButton("✏️ Ответить",
                                 callback_data=f"manual_reply_{account['id']}_{chat_id_avito}"),
            InlineKeyboardButton("📜 История", callback_data=f"history_{account['id']}_{chat_id_avito}")
        ],
        [InlineKeyboardButton("📝 Шаблоны >",
                              callback_data=f"canned_start_{account['id']}_{chat_id_avito}")]
    ]
    if account['ai_mode'] in [1, 2]:
        keyboard.append([InlineKeyboardButton("🤖 Ответить с AI",
                                              callback_data=f"ai_reply_{account['id']}_{chat_id_avito}")])
    return InlineKeyboardMarkup(keyboard)


def escape_markdown_v2(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


async def check_avito_messages(context: ContextTypes.DEFAULT_TYPE):
    status_data = load_json(STATUS_FILE, {'status': 'stopped'})
    if status_data.get('status') != 'running':
        logger.info("Проверка сообщений пропущена, так как бот остановлен.")
        return

    logger.info("Начинаю проверку сообщений Avito...")
    last_timestamps = load_json(LAST_TIMESTAMPS_FILE)
    active_accounts = db.get_accounts(active_only=True)
    ai_settings = load_json(AI_SETTINGS_FILE, {})

    active_period_days = int(context.bot_data['config']['SETTINGS'].get('ACTIVE_PERIOD_DAYS', 30))
    archive_boundary_ts = int(time.time()) - (active_period_days * 24 * 60 * 60)

    for account in active_accounts:
        account_name = account['name']
        account_id_str = str(account['id'])
        token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])

        if not token:
            try:
                await context.bot.send_message(
                    chat_id=context.bot_data['config']['TELEGRAM']['ALLOWED_USER_IDS'].split(',')[0].strip(),
                    text=f"⚠️ Ошибка получения токена Avito для аккаунта «{account_name}»! Проверьте Client ID и Secret.")
            except Exception as bot_e:
                logger.error(f"Не удалось отправить уведомление об ошибке токена: {bot_e}")
            continue

        stop_fetching = False
        offset = 0
        limit = 50
        recent_chats_list = []

        while not stop_fetching:
            chats_batch = await asyncio.to_thread(avito.get_chats, token, account['profile_id'], limit, offset)

            if chats_batch is None:
                logger.warning(
                    f"Ошибка API при получении чатов для '{account_name}'. Возможно, токен невалиден. Очищаю кэш.")
                await asyncio.to_thread(avito.clear_token, account['client_id'])
                break

            if not chats_batch:
                break

            for chat in chats_batch:
                last_message_ts = chat.get('last_message', {}).get('created', 0)
                if last_message_ts < archive_boundary_ts:
                    stop_fetching = True
                    break
                recent_chats_list.append(chat)

            if len(chats_batch) < limit:
                break
            offset += limit

        if not recent_chats_list and offset == 0:
            continue

        unanswered_count = sum(1 for chat in recent_chats_list if chat.get('last_message', {}).get('direction') == 'in')
        context.bot_data[f"unanswered_count_{account_id_str}"] = unanswered_count
        logger.info(
            f"Аккаунт '{account_name}': Найдено {len(recent_chats_list)} активных чатов. ({unanswered_count} неотвеченных).")

        account_timestamps = last_timestamps.setdefault(account_id_str, {})
        is_initial_run = not bool(account_timestamps)

        for chat in recent_chats_list:
            chat_id_avito = chat['id']
            try:
                new_messages = []
                time.sleep(0.2)

                messages = await asyncio.to_thread(avito.get_messages, token, account['profile_id'], chat_id_avito)
                if messages is None:
                    logger.warning(f"Не удалось получить сообщения для чата {chat_id_avito}, пропуск.")
                    continue

                incoming_messages = sorted(
                    [msg for msg in messages if msg.get('direction') == 'in'],
                    key=lambda x: x.get('created', 0)
                )

                if not incoming_messages:
                    continue

                last_message_ts = incoming_messages[-1].get('created', 0)
                last_known_ts = account_timestamps.get(chat_id_avito, 0)

                if is_initial_run:
                    account_timestamps[chat_id_avito] = last_message_ts
                    continue

                if last_message_ts <= last_known_ts:
                    continue

                new_messages = [msg for msg in incoming_messages if msg.get('created', 0) > last_known_ts]

                if new_messages:
                    for msg in new_messages:
                        if msg.get('type') != 'text':
                            logger.info(
                                f"Пропущено системное сообщение типа '{msg.get('type')}' в чате {chat_id_avito}")
                            continue

                        text = msg.get('content', {}).get('text', '')
                        user_info = chat.get('users', [{}])[0]
                        author_name = user_info.get('name', 'Неизвестно')
                        author_id = user_info.get('id', 'N/A')
                        author_str = f"{author_name} ({author_id})"
                        ad_context = chat.get('context', {}).get('value', {})
                        ad_title = ad_context.get('title', 'Не указано')
                        msg_datetime = datetime.fromtimestamp(msg.get('created', 0), timezone.utc) + timedelta(hours=3)
                        date_str = msg_datetime.strftime('%d.%m.%Y, %H:%M')

                        message_text = (
                            f"📬 *Новое сообщение для «{escape_markdown_v2(account_name)}»*\n\n"
                            f"*От:* {escape_markdown_v2(author_str)}\n"
                            f"*Объявление:* {escape_markdown_v2(ad_title)}\n\n"
                            f"*Текст:* {escape_markdown_v2(text)}\n"
                            f"*Дата:* {escape_markdown_v2(date_str)}"
                        )

                        db.log_message(account['id'], chat_id_avito, 'in', None, text)
                        reply_markup = _build_chat_interaction_keyboard(account, chat_id_avito)

                        sent_message = await context.bot.send_message(
                            chat_id=account['notification_chat_id'],
                            text=message_text,
                            parse_mode='MarkdownV2',
                            reply_markup=reply_markup
                        )
                        logger.info(f"Сообщение из чата {chat_id_avito} переслано в Telegram.")

                        if account['ai_mode'] > 0:
                            delay_minutes = account.get('ai_reply_delay') or ai_settings.get('global_ai_reply_delay', 1)
                            delay_seconds = int(delay_minutes) * 60

                            logger.info(f"Планирую авто-ответ для чата {chat_id_avito} через {delay_minutes} мин.")
                            job_data = {
                                "account_id": account['id'],
                                "chat_id_avito": chat_id_avito,
                                "reply_to_message_id": sent_message.message_id
                            }
                            job_name = f"ai_reply_{chat_id_avito}"
                            current_jobs = context.job_queue.get_jobs_by_name(job_name)
                            for job in current_jobs:
                                job.schedule_removal()
                            context.job_queue.run_once(ai_auto_reply_job, delay_seconds, data=job_data, name=job_name)

                        await asyncio.sleep(1)

                account_timestamps[chat_id_avito] = last_message_ts

            except Exception as e:
                logger.warning(f"Не удалось обработать чат {chat_id_avito}: {e}")
                continue

        if is_initial_run:
            logger.info(f"Первичная настройка для аккаунта '{account_name}' завершена.")

        save_json(LAST_TIMESTAMPS_FILE, last_timestamps)
    logger.info("Проверка сообщений завершена.")

async def _send_automation_settings_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    account_id = context.user_data.get('account_id')
    acc = db.get_account_by_id(account_id)

    ai_mode_map = {
        0: "⚪️ Выключен", 1: "🤖¹ ИИ-Ограниченный", 2: "🤖² ИИ-Полный",
        3: "📝¹ Автоответчик-Ограниченный", 4: "📝² Автоответчик-Полный"
    }
    ai_status_text = ai_mode_map.get(acc['ai_mode'], "N/A")
    ai_delay_text = f"{acc['ai_reply_delay']} мин." if acc.get('ai_reply_delay') else "Глобальная"
    category_name = acc.get('default_category_name') or "Не выбрана"
    auto_reply_template = acc.get('auto_reply_template_name') or "Не выбран"

    text = (f"<b>⚙️ Настройки автоматизации «{html.escape(acc['name'])}»</b>\n\n"
            f"<b>Режим автоответа:</b> {ai_status_text}\n")

    if acc['ai_mode'] > 0:
        text += f"<b>Время ожидания:</b> {ai_delay_text}\n"

    if acc['ai_mode'] in [3, 4]:
        text += f"<b>Шаблон автоответа:</b> {html.escape(auto_reply_template)}\n"
    elif acc['ai_mode'] in [1, 2]:
        prompt_lim = acc.get('prompt_name_limited') or "По умолчанию"
        prompt_full = acc.get('prompt_name_full') or "По умолчанию"
        text += (f"<b>Провайдер ИИ:</b> <code>{acc['ai_provider']}</code>\n"
                 f"<b>Промпт (Огранич.):</b> <code>{html.escape(prompt_lim)}</code>\n"
                 f"<b>Промпт (Полный):</b> <code>{html.escape(prompt_full)}</code>\n")

    text += f"<b>Категория для кнопок:</b> {html.escape(category_name)}"

    keyboard = [
        [InlineKeyboardButton("⚙️ Режим автоответа", callback_data=f"choose_ai_mode_{acc['id']}")],
    ]

    if acc['ai_mode'] > 0:
        keyboard.append([InlineKeyboardButton("🕒 Время ожидания", callback_data=f"set_ai_delay_{acc['id']}")])

    if acc['ai_mode'] in [3, 4]:
        keyboard.append([InlineKeyboardButton("📝 Выбрать шаблон автоответа",
                                              callback_data=f"choose_autoreply_template_{acc['id']}_0")])
    elif acc['ai_mode'] in [1, 2]:
        keyboard.append([
            InlineKeyboardButton("Промпты ИИ", callback_data=f"choose_prompt_type_{acc['id']}"),
            InlineKeyboardButton("🌐 Провайдер AI", callback_data=f"choose_provider_acc_{acc['id']}")
        ])

    keyboard.extend([
        [InlineKeyboardButton("🗂️ Категория для кнопок", callback_data=f"choose_cat_acc_{acc['id']}_0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_{acc['id']}")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML)

async def _send_templates_show_categories_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                                               message_id: int = None):
    page = 0
    categories = db.get_categories()
    paginated_items, total_items = get_paginated_items(categories, page, 10)

    text = "🗂️ <b>Категории шаблонов</b>\n\nВыберите категорию для просмотра шаблонов:"
    keyboard = []
    if not paginated_items:
        text = "У вас еще нет категорий."

    for cat in paginated_items:
        keyboard.append([InlineKeyboardButton(cat['name'], callback_data=f"cat_view_{cat['id']}_0")])

    total_pages = (total_items + 9) // 10
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"templates_show_categories_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"templates_show_categories_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("➕ Добавить категорию", callback_data="add_category_start")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="templates_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML)


async def _send_templates_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    keyboard = [
        [InlineKeyboardButton("📋 Мои шаблоны", callback_data="templates_show_categories_0")],
        [InlineKeyboardButton("➕ Добавить шаблон", callback_data="add_template_start")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Управление шаблонами быстрых ответов:"

    if message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def ai_auto_reply_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    account_id = job_data['account_id']
    chat_id_avito = job_data['chat_id_avito']
    reply_to_message_id = job_data.get('reply_to_message_id')

    account = db.get_account_by_id(account_id)
    if not account or not account['is_active'] or account['ai_mode'] == 0:
        logger.info(f"AI Auto-Reply отменен: аккаунт {account_id} неактивен или режим выключен.")
        return

    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])
    if not token:
        logger.error(f"AI Auto-Reply: Не удалось получить токен для {account['name']}")
        return

    messages = await asyncio.to_thread(avito.get_messages, token, account['profile_id'], chat_id_avito)
    if messages is None:
        logger.warning(f"AI Auto-Reply: не удалось получить сообщения для чата {chat_id_avito}, отмена.")
        await asyncio.to_thread(avito.clear_token, account['client_id'])
        return

    last_message = sorted(messages, key=lambda x: x.get('created', 0))[-1]
    if last_message['direction'] == 'out':
        logger.info(f"AI Auto-Reply для чата {chat_id_avito} отменен: ответ уже был отправлен.")
        return

    if account['ai_mode'] in [1, 3]:
        incoming_messages_count = sum(1 for m in messages if m['direction'] == 'in')
        if incoming_messages_count > 1:
            logger.info(
                f"Ограниченный автоответчик для чата {chat_id_avito} отменен: в чате более одного входящего сообщения.")
            return

    if account['ai_mode'] in [3, 4]:
        template_id = account.get('auto_reply_template_id')
        if not template_id:
            logger.warning(f"Автоответчик для '{account['name']}' включен, но шаблон не выбран.")
            return

        template = db.get_canned_response_by_id(template_id)
        if not template:
            logger.error(f"Не удалось найти шаблон с ID {template_id} для автоответчика.")
            return

        response_text = template['response_text']
        reply_type = 'template_auto'
        notification_text = f"📝 *Автоответчик отправил ответ для «{escape_markdown_v2(account['name'])}»*\n\n{escape_markdown_v2(response_text)}"

    elif account['ai_mode'] in [1, 2]:
        settings = load_json(AI_SETTINGS_FILE, {})
        api_key = settings.get('api_keys', {}).get(account['ai_provider'])
        if not api_key:
            logger.error(f"AI Auto-Reply: API ключ для {account['ai_provider']} не найден.")
            return

        prompt_text = account.get('prompt_text_full') or DEFAULT_PROMPT
        if account['ai_mode'] == 1 and account.get('prompt_text_limited'):
            prompt_text = account['prompt_text_limited']

        history = await asyncio.to_thread(avito.get_chat_history, token, account['profile_id'], chat_id_avito)
        ai_response = await avito.generate_ai_reply(history, api_key, account['ai_provider'], prompt_text)

        if not ai_response or not ai_response.strip():
            logger.error(f"AI Auto-Reply: Получен пустой ответ для чата {chat_id_avito}")
            return

        response_text = ai_response
        reply_type = 'ai'
        notification_text = f"🤖 *ИИ автоматически ответил для «{escape_markdown_v2(account['name'])}»*\n\n{escape_markdown_v2(response_text)}"

    else:
        return

    try:
        await asyncio.to_thread(avito.send_message, token, account['profile_id'], chat_id_avito, response_text)
        db.log_message(account['id'], chat_id_avito, 'out', reply_type, response_text)
    except Exception as e:
        logger.error(f"Авто-ответ: Не удалось отправить сообщение в чат Avito {chat_id_avito}: {e}")
        return

    try:
        await context.bot.send_message(
            chat_id=account['notification_chat_id'],
            text=notification_text,
            parse_mode='MarkdownV2',
            reply_to_message_id=reply_to_message_id
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление об авто-ответе менеджеру в Telegram: {e}")


def is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed_ids = [int(uid.strip()) for uid in context.bot_data['config']['TELEGRAM']['ALLOWED_USER_IDS'].split(',')]
    return update.effective_user.id in allowed_ids


def get_paginated_items(items, page, items_per_page=ITEMS_PER_PAGE):
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    return items[start_idx:end_idx], len(items)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update, context):
        await update.message.reply_text("❌ Доступ запрещен.")
        return ConversationHandler.END

    status_data = load_json(STATUS_FILE, {'status': 'stopped'})
    is_running = status_data.get('status') == 'running'
    status_icon = "🟢 Работает"
    if not is_running:
        status_icon = "🔴 Остановлен"

    toggle_button_text = "⏹️ Остановить" if is_running else "▶️ Запустить"
    toggle_button_callback = "stop_polling" if is_running else "start_polling"

    all_accounts = db.get_accounts()
    active_accounts = [acc for acc in all_accounts if acc['is_active']]
    stats_today = db.get_stats_for_period('day')

    account_info_blocks = []
    if active_accounts:
        for acc in active_accounts:
            acc_id_str = str(acc['id'])
            received = sum(1 for log in stats_today if log['account_id'] == acc['id'] and log['direction'] == 'in')
            replied = sum(1 for log in stats_today if log['account_id'] == acc['id'] and log['direction'] == 'out')
            unanswered = context.bot_data.get(f"unanswered_count_{acc_id_str}", "...")

            ai_mode_map = {
                0: "⚪️ Выключен", 1: "🤖¹ Ограниченный", 2: "🤖² Полный",
                3: "📝¹ Ограниченный", 4: "📝² Полный"
            }
            ai_status_text = ai_mode_map.get(acc.get('ai_mode', 0), "⚪️")

            template_count = 0
            if acc.get('default_category_id'):
                templates_in_category = db.get_canned_responses_by_category(acc['default_category_id'])
                template_count = len(templates_in_category)

            account_name = html.escape(acc.get('name', 'Безымянный'))

            account_info_blocks.append(
                f"<b>{account_name}</b>\n"
                f"  - <i>Режим автоответа:</i> {ai_status_text}\n"
                f"  - <i>Шаблонов в категории:</i> {template_count}\n"
                f"  - <i>Статистика:</i> 📥{received} / 📤{replied} | ⌛️: <b>{unanswered}</b>"
            )

    accounts_text = "\n\n".join(account_info_blocks)

    info_text = ""
    if accounts_text:
        info_text = f"<b><u>Активные аккаунты:</u></b>\n{accounts_text}"
    else:
        info_text = "<i>Нет активных аккаунтов.</i>"

    keyboard = [
        [InlineKeyboardButton("👤 Аккаунты", callback_data="accounts_menu")],
        [InlineKeyboardButton("📝 Шаблоны ответов", callback_data="templates_menu")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats_menu"),
         InlineKeyboardButton("⚙️ Настройки AI", callback_data="ai_settings_menu")],
        [InlineKeyboardButton("🔎 Поиск по чатам", callback_data="search_start")],
        [InlineKeyboardButton(toggle_button_text, callback_data=toggle_button_callback)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"<b>--- Avito Manager Bot ---</b>\n<b>Статус:</b> {status_icon}\n\n{info_text}\n\nВыберите действие:"

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except BadRequest:
            pass
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except BadRequest:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup,
                                           parse_mode=ParseMode.HTML)
            await update.callback_query.message.delete()
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    return MAIN_MENU

async def accounts_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    keyboard = [
        [InlineKeyboardButton("📋 Мои аккаунты", callback_data="my_accounts")],
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account_start")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    await query.edit_message_text("Управление аккаунтами Avito:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ACCOUNTS_MENU


async def start_polling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    save_json(STATUS_FILE, {'status': 'running'})
    try:
        await query.answer("✅ Проверка сообщений запущена.", show_alert=True)
    except BadRequest:
        pass
    return await start(update, context)


async def stop_polling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    save_json(STATUS_FILE, {'status': 'stopped'})
    try:
        await query.answer("✅ Проверка сообщений остановлена.", show_alert=True)
    except BadRequest:
        pass
    return await start(update, context)


async def _send_account_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    account_id = context.user_data.get('account_id')
    if not account_id:
        return

    acc = db.get_account_by_id(account_id)
    if not acc:
        await context.bot.send_message(chat_id, "❌ Аккаунт не найден.")
        return

    status_text = "🟢 Включен" if acc['is_active'] else "🔴 Отключен"
    toggle_text = "🔴 Отключить" if acc['is_active'] else "🟢 Включить"
    ai_mode_map = {
        0: "⚪️ Выключен", 1: "🤖¹ ИИ-Ограниченный", 2: "🤖² ИИ-Полный",
        3: "📝¹ Автоответчик-Ограниченный", 4: "📝² Автоответчик-Полный"
    }
    ai_status_text = ai_mode_map.get(acc['ai_mode'], "N/A")

    text = (f"<b>Управление «{html.escape(acc['name'])}»</b>\n\n"
            f"<b>Статус:</b> {status_text}\n"
            f"<b>Режим автоответа:</b> {ai_status_text}\n\n"
            "Выберите раздел для редактирования:")

    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data=f"toggle_status_{acc['id']}")],
        [InlineKeyboardButton("⚙️ Настройки автоматизации", callback_data="automation_settings_menu")],
        [InlineKeyboardButton("🗂️ Данные аккаунта", callback_data="account_data_menu")],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data="my_accounts")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML)


async def _send_account_data_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    account_id = context.user_data.get('account_id')
    acc = db.get_account_by_id(account_id)

    def mask_secret(secret: str) -> str:
        if not secret or len(secret) < 5:
            return "не задан"
        return f"{secret[:4]}...{secret[-4:]}"

    text = (
        f"<b>🗂️ Данные аккаунта «{html.escape(acc['name'])}»</b>\n\n"
        f"<b>Имя:</b> <code>{html.escape(acc['name'])}</code>\n"
        f"<b>Client ID:</b> <code>{html.escape(acc['client_id'])}</code>\n"
        f"<b>Client Secret:</b> <code>{html.escape(mask_secret(acc['client_secret']))}</code>\n"
        f"<b>Profile ID:</b> <code>{html.escape(acc['profile_id'])}</code>\n"
        f"<b>Чат ID уведомлений:</b> <code>{acc['notification_chat_id']}</code>\n\n"
        "Нажмите на кнопку, чтобы изменить соответствующее поле:"
    )

    keyboard = [
        [InlineKeyboardButton("Изменить Имя", callback_data="edit_field_name")],
        [InlineKeyboardButton("Изменить Client ID", callback_data="edit_field_client_id")],
        [InlineKeyboardButton("Изменить Client Secret", callback_data="edit_field_client_secret")],
        [InlineKeyboardButton("Изменить Profile ID", callback_data="edit_field_profile_id")],
        [InlineKeyboardButton("Изменить Чат ID", callback_data="edit_field_notification_chat_id")],
        [InlineKeyboardButton("❌ Удалить аккаунт", callback_data=f"delete_account_confirm_{account_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_{account_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                                reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                           parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML)


async def my_accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    accounts = db.get_accounts()
    keyboard = []
    text = "У вас еще нет добавленных аккаунтов."
    if accounts:
        text = "Выберите аккаунт для управления:"
        for acc in accounts:
            status_icon = "🟢" if acc['is_active'] else "🔴"
            ai_mode_map = {0: "⚪️", 1: "🤖¹", 2: "🤖²", 3: "📝"}
            ai_icon = ai_mode_map.get(acc.get('ai_mode', 0), "⚪️")
            keyboard.append(
                [InlineKeyboardButton(f"{status_icon}{ai_icon} {html.escape(acc['name'])}",
                                      callback_data=f"edit_{acc['id']}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="accounts_menu")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ACCOUNT_LIST


async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    context.user_data.clear()

    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите имя для этого аккаунта (например, 'Основной рабочий'):",
        reply_markup=CANCEL_KEYBOARD
    )
    return ADD_ACCOUNT_NAME


async def add_account_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Теперь введите Client ID от Avito API:", reply_markup=CANCEL_KEYBOARD)
    return ADD_ACCOUNT_CLIENT_ID


async def add_account_get_client_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['client_id'] = update.message.text.strip()
    await update.message.reply_text("Отлично, теперь Client Secret:", reply_markup=CANCEL_KEYBOARD)
    return ADD_ACCOUNT_CLIENT_SECRET


async def add_account_get_client_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['client_secret'] = update.message.text.strip()
    await update.message.reply_text("Теперь ID вашего профиля Avito:", reply_markup=CANCEL_KEYBOARD)
    return ADD_ACCOUNT_PROFILE_ID


async def add_account_get_profile_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['profile_id'] = update.message.text.strip()
    await update.message.reply_text("И последнее: ID чата в Telegram, куда присылать уведомления:",
                                    reply_markup=CANCEL_KEYBOARD)
    return ADD_ACCOUNT_CHAT_ID


async def add_account_get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['chat_id'] = int(update.message.text)
        db.add_account(context.user_data)
        await update.message.reply_text(f"✅ Аккаунт «{html.escape(context.user_data['name'])}» успешно добавлен!",
                                        reply_markup=ReplyKeyboardRemove())
    except (ValueError, TypeError):
        await update.message.reply_text("ID чата должен быть числом. Попробуйте снова:", reply_markup=CANCEL_KEYBOARD)
        return ADD_ACCOUNT_CHAT_ID
    except sqlite3.IntegrityError:
        await update.message.reply_text("Аккаунт с таким именем уже существует. Попробуйте другое имя.",
                                        reply_markup=ReplyKeyboardRemove())
    finally:
        context.user_data.clear()

    return await start(update, context)


async def edit_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass

    if 'edit_' in query.data:
        account_id = int(query.data.split('_')[1])
        context.user_data['account_id'] = account_id

    if not context.user_data.get('account_id'):
        return await start(update, context)

    await _send_account_menu(query.message.chat_id, context, query.message.message_id)
    return EDIT_ACCOUNT_MENU

async def account_data_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_account_data_menu(query.message.chat_id, context, query.message.message_id)
    return ACCOUNT_DATA_MENU

async def automation_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_automation_settings_menu(query.message.chat_id, context, query.message.message_id)
    return AUTOMATION_SETTINGS_MENU


async def delete_account_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    account_id = int(query.data.split('_')[-1])
    account = db.get_account_by_id(account_id)
    if not account:
        await query.answer("Аккаунт уже удален.", show_alert=True)
        return await my_accounts_menu(update, context)

    text = f"Вы уверены, что хотите удалить аккаунт «{html.escape(account['name'])}»?\n\n<b>Это действие необратимо!</b>"
    keyboard = [
        [InlineKeyboardButton(f"Да, удалить «{html.escape(account['name'])}»",
                              callback_data=f"delete_account_execute_{account_id}")],
        [InlineKeyboardButton("Нет, вернуться назад", callback_data=f"account_data_menu")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return DELETE_ACCOUNT_CONFIRM


async def delete_account_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    account_id = int(query.data.split('_')[-1])
    db.delete_account(account_id)
    await query.answer("Аккаунт успешно удален", show_alert=True)
    context.user_data.pop('account_id', None)
    return await my_accounts_menu(update, context)


async def toggle_account_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    account_id = int(parts[2])
    context.user_data['account_id'] = account_id

    acc = db.get_account_by_id(account_id)
    if acc:
        new_status = not acc['is_active']
        db.update_account(account_id, 'is_active', new_status)
        try:
            await query.answer("Статус обновлен", show_alert=False)
        except BadRequest:
            pass

    return await edit_account_menu(update, context)


async def edit_account_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    field = '_'.join(query.data.split('_')[2:])
    context.user_data['editing_field'] = field

    field_map = {
        'name': 'имя аккаунта', 'client_id': 'Client ID', 'client_secret': 'Client Secret',
        'profile_id': 'Profile ID', 'notification_chat_id': 'ID чата для уведомлений'
    }

    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Введите новое значение для «{field_map.get(field, field)}»:",
        reply_markup=CANCEL_KEYBOARD
    )
    return EDIT_ACCOUNT_FIELD


async def save_account_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = update.message.text.strip()
    field = context.user_data.get('editing_field')
    account_id = context.user_data.get('account_id')

    if not field or not account_id:
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")
        return await start(update, context)

    if field == 'notification_chat_id':
        try:
            new_value = int(new_value)
        except (ValueError, TypeError):
            await update.message.reply_text("ID чата должно быть числом. Попробуйте снова:",
                                            reply_markup=CANCEL_KEYBOARD)
            return EDIT_ACCOUNT_FIELD

    db.update_account(account_id, field, new_value)
    await update.message.reply_text("✅ Данные обновлены.", reply_markup=ReplyKeyboardRemove())

    context.user_data.pop('editing_field', None)

    await _send_account_data_menu(update.effective_chat.id, context)
    return ACCOUNT_DATA_MENU


async def choose_ai_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = context.user_data.get('account_id')

    if not account_id:
        await query.edit_message_text(
            "❗️ Контекст утерян. Пожалуйста, вернитесь в главное меню.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]])
        )
        return MAIN_MENU

    keyboard = [
        [InlineKeyboardButton("⚪️ Без ИИ и автоответчика", callback_data="set_ai_mode_0")],
        [InlineKeyboardButton("🤖¹ ИИ-Ограниченный", callback_data="set_ai_mode_1")],
        [InlineKeyboardButton("🤖² ИИ-Полный", callback_data="set_ai_mode_2")],
        [InlineKeyboardButton("📝¹ Автоответчик-Ограниченный", callback_data="set_ai_mode_3")],
        [InlineKeyboardButton("📝² Автоответчик-Полный", callback_data="set_ai_mode_4")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="automation_settings_menu")]
    ]
    await query.edit_message_text("Выберите режим работы автоответа для этого аккаунта:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_AI_MODE


async def set_ai_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = int(query.data.split('_')[-1])
    account_id = context.user_data.get('account_id')

    db.update_account(account_id, 'ai_mode', mode)

    await query.answer("Режим автоответа обновлен", show_alert=True)

    await _send_automation_settings_menu(query.message.chat_id, context, query.message.message_id)
    return AUTOMATION_SETTINGS_MENU


async def set_ai_delay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите задержку ответа ИИ в минутах (целое число, мин. 1).\nИли введите 0, чтобы использовать глобальную настройку.",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_AI_DELAY


async def save_ai_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account_id = context.user_data.get('account_id')
    try:
        delay = int(update.message.text.strip())
        if delay == 0:
            db.update_account(account_id, 'ai_reply_delay', None)
            await update.message.reply_text("✅ Задержка сброшена на глобальную.", reply_markup=ReplyKeyboardRemove())
        elif delay > 0:
            db.update_account(account_id, 'ai_reply_delay', delay)
            await update.message.reply_text(f"✅ Задержка установлена на {delay} мин.",
                                            reply_markup=ReplyKeyboardRemove())
        else:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("Неверное значение. Введите целое число, 0 или больше.",
                                        reply_markup=CANCEL_KEYBOARD)
        return AWAITING_AI_DELAY

    await _send_automation_settings_menu(update.effective_chat.id, context)
    return AUTOMATION_SETTINGS_MENU


async def choose_autoreply_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    account_id = int(parts[3])
    page = int(parts[4])

    templates = db.get_canned_responses()
    paginated_templates, total_items = get_paginated_items(templates, page)

    keyboard = []
    text = "Выберите шаблон для автоответчика:"
    if not paginated_templates:
        text = "Сначала добавьте хотя бы один шаблон."

    for t in paginated_templates:
        keyboard.append([InlineKeyboardButton(t['short_name'], callback_data=f"set_autoreply_template_{t['id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"choose_autoreply_template_{account_id}_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"choose_autoreply_template_{account_id}_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="automation_settings_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_AUTOREPLY_TEMPLATE


async def set_autoreply_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    template_id = int(query.data.split('_')[-1])
    account_id = context.user_data.get('account_id')

    db.update_account(account_id, 'auto_reply_template_id', template_id)
    await query.answer("✅ Шаблон для автоответчика установлен", show_alert=True)

    await _send_automation_settings_menu(query.message.chat_id, context, query.message.message_id)
    return AUTOMATION_SETTINGS_MENU


async def choose_prompt_type_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = context.user_data.get('account_id')

    keyboard = [
        [InlineKeyboardButton("Для 'Ограниченного ИИ'", callback_data="choose_prompt_for_limited_0")],
        [InlineKeyboardButton("Для 'Полного ИИ'", callback_data="choose_prompt_for_full_0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_{account_id}")]
    ]
    await query.edit_message_text("Выберите, для какого режима ИИ вы хотите установить промпт:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_PROMPT_TYPE_FOR_ACCOUNT


async def choose_prompt_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    prompt_type = parts[3]
    page = int(parts[4])
    context.user_data['prompt_type_to_set'] = prompt_type

    account_id = context.user_data['account_id']

    prompts = db.get_prompts()
    paginated_prompts, total_items = get_paginated_items(prompts, page)

    keyboard = []
    for p in paginated_prompts:
        keyboard.append([InlineKeyboardButton(p['name'], callback_data=f"set_prompt_{p['id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"choose_prompt_for_{prompt_type}_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"choose_prompt_for_{prompt_type}_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🗑️ Сбросить (по умолчанию)", callback_data="set_prompt_0")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"choose_prompt_type_{account_id}")])
    await query.edit_message_text("Выберите промпт:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_PROMPT_FOR_ACCOUNT


async def set_prompt_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    prompt_id = int(query.data.split('_')[-1])
    account_id = context.user_data['account_id']
    prompt_type = context.user_data.get('prompt_type_to_set')

    field_to_update = f"prompt_id_{prompt_type}"

    db.update_account(account_id, field_to_update, prompt_id if prompt_id > 0 else None)

    try:
        await query.answer("✅ Промпт для аккаунта обновлен", show_alert=True)
    except BadRequest:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Промпт для аккаунта обновлен.")

    context.user_data.pop('prompt_type_to_set', None)
    await _send_automation_settings_menu(query.message.chat_id, context, query.message.message_id)
    return AUTOMATION_SETTINGS_MENU


async def choose_category_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    account_id = int(parts[3])
    page = int(parts[4])

    categories = db.get_categories()
    paginated_cats, total_items = get_paginated_items(categories, page)

    keyboard = []
    for cat in paginated_cats:
        keyboard.append([InlineKeyboardButton(cat['name'], callback_data=f"set_cat_acc_{cat['id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"choose_cat_acc_{account_id}_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"choose_cat_acc_{account_id}_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🗑️ Сбросить (без категории)", callback_data="set_cat_acc_0")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="automation_settings_menu")])

    await query.edit_message_text("Выберите категорию шаблонов по умолчанию для этого аккаунта:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_CATEGORY_FOR_ACCOUNT


async def set_category_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category_id = int(query.data.split('_')[-1])
    account_id = context.user_data['account_id']

    db.update_account(account_id, 'default_category_id', category_id if category_id > 0 else None)
    await query.answer("✅ Категория по умолчанию обновлена", show_alert=True)

    return await edit_account_menu(update, context)


async def canned_response_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    account_id = int(parts[2])
    avito_chat_id = '_'.join(parts[3:])
    page = 0

    account = db.get_account_by_id(account_id)

    if account and account.get('default_category_id'):
        category_id = account['default_category_id']
        templates = db.get_canned_responses_by_category(category_id)
        paginated_items, total_items = get_paginated_items(templates, page)

        keyboard = []
        for tmpl in paginated_items:
            keyboard.append([InlineKeyboardButton(tmpl['short_name'],
                                                  callback_data=f"send_canned_{tmpl['id']}_{account_id}_{avito_chat_id}")])

        nav_buttons = []
        if (page + 1) * ITEMS_PER_PAGE < total_items:
            nav_buttons.append(InlineKeyboardButton("➡️",
                                                    callback_data=f"tmpl_list_{category_id}_{account_id}_{avito_chat_id}_{page + 1}"))

        if nav_buttons:
            keyboard.append(nav_buttons)

        keyboard.append([InlineKeyboardButton("🔙 Назад",
                                              callback_data=f"restore_buttons_{account_id}_{avito_chat_id}")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    else:
        categories = db.get_categories()
        paginated_items, total_items = get_paginated_items(categories, page)

        keyboard = []
        for cat in paginated_items:
            keyboard.append(
                [InlineKeyboardButton(cat['name'],
                                      callback_data=f"tmpl_list_{cat['id']}_{account_id}_{avito_chat_id}_0")])

        nav_buttons = []
        if (page + 1) * ITEMS_PER_PAGE < total_items:
            nav_buttons.append(
                InlineKeyboardButton("➡️", callback_data=f"cat_list_{account_id}_{avito_chat_id}_{page + 1}"))

        if nav_buttons:
            keyboard.append(nav_buttons)

        original_keyboard = query.message.reply_markup.inline_keyboard
        back_button_row = []
        for btn_row in original_keyboard:
            if any("manual_reply" in btn.callback_data for btn in btn_row):
                back_button_row = btn_row
                break
        if back_button_row:
            keyboard.append(back_button_row)
        else:
            keyboard.append([InlineKeyboardButton("Отмена", callback_data="delete_message")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

async def restore_original_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    account_id = int(parts[2])
    avito_chat_id = '_'.join(parts[3:])

    account = db.get_account_by_id(account_id)
    if not account:
        await query.edit_message_text("❌ Ошибка: аккаунт не найден.")
        return

    original_keyboard = _build_chat_interaction_keyboard(account, avito_chat_id)
    await query.edit_message_reply_markup(reply_markup=original_keyboard)

async def request_chat_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("⏳ Загружаю историю...")
    except BadRequest:
        logger.info(f"Не удалось ответить на callback_query (возможно, он устарел).")

    parts = query.data.split('_')
    account_id = int(parts[1])
    avito_chat_id = '_'.join(parts[2:])

    account = db.get_account_by_id(account_id)
    if not account:
        await query.message.reply_text("❌ Аккаунт не найден.")
        return

    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])
    if not token:
        await query.message.reply_text("❌ Ошибка авторизации Avito.")
        return

    try:
        history = await asyncio.to_thread(avito.get_chat_history, token, account['profile_id'], avito_chat_id)
        if not history:
            history = "В этом чате пока нет сообщений."

        await query.message.reply_text(
            f"<b>История чата (последние 10 сообщений):</b>\n\n<pre>{html.escape(history)}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скрыть", callback_data="delete_message")]]))
    except Exception as e:
        logger.error(f"Не удалось получить историю чата {avito_chat_id}: {e}")
        await query.message.reply_text(f"❌ Не удалось получить историю чата: {e}")


async def manual_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    parts = query.data.split('_')
    account_id = int(parts[2])
    avito_chat_id = '_'.join(parts[3:])

    context.user_data['reply_account_id'] = account_id
    context.user_data['reply_avito_chat_id'] = avito_chat_id

    await query.message.reply_text(
        text="Пожалуйста, введите ваш ответ на это сообщение:",
        reply_markup=ForceReply(selective=True)
    )
    return AWAITING_MANUAL_REPLY


async def manual_reply_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_text = update.message.text
    account_id = context.user_data.get('reply_account_id')
    avito_chat_id = context.user_data.get('reply_avito_chat_id')

    if not all([account_id, avito_chat_id]):
        await update.message.reply_text("Произошла ошибка: не найдены данные для ответа. Попробуйте снова.")
        context.user_data.clear()
        return ConversationHandler.END

    account = db.get_account_by_id(account_id)
    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])

    try:
        await asyncio.to_thread(avito.send_message, token, account['profile_id'], avito_chat_id, reply_text)
        await update.message.reply_text("✅ Ваш ответ успешно отправлен на Avito.")
        db.log_message(account_id, avito_chat_id, 'out', 'manual', reply_text)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось отправить ответ: {e}")
        logger.error(f"Ошибка отправки ручного ответа: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def show_categories_for_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    parts = query.data.split('_')
    account_id = int(parts[2])
    page = int(parts[-1])
    avito_chat_id = '_'.join(parts[3:-1])

    categories = db.get_categories()
    paginated_items, total_items = get_paginated_items(categories, page)

    keyboard = []
    for cat in paginated_items:
        keyboard.append(
            [InlineKeyboardButton(cat['name'], callback_data=f"tmpl_list_{cat['id']}_{account_id}_{avito_chat_id}_0")])

    nav_buttons = []
    if page > 0: nav_buttons.append(
        InlineKeyboardButton("⬅️", callback_data=f"cat_list_{account_id}_{avito_chat_id}_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items: nav_buttons.append(
        InlineKeyboardButton("➡️", callback_data=f"cat_list_{account_id}_{avito_chat_id}_{page + 1}"))

    if nav_buttons: keyboard.append(nav_buttons)

    original_keyboard = query.message.reply_markup.inline_keyboard

    back_button_row = []
    for btn_row in original_keyboard:
        if any("manual_reply" in btn.callback_data for btn in btn_row):
            back_button_row = btn_row
            break
    if back_button_row:
        keyboard.append(back_button_row)
    else:
        keyboard.append([InlineKeyboardButton("Отмена", callback_data="delete_message")])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def show_templates_for_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    parts = query.data.split('_')
    category_id = int(parts[2])
    account_id = int(parts[3])
    page = int(parts[-1])
    avito_chat_id = '_'.join(parts[4:-1])

    templates = db.get_canned_responses_by_category(category_id)
    paginated_items, total_items = get_paginated_items(templates, page)

    keyboard = []
    for tmpl in paginated_items:
        keyboard.append([InlineKeyboardButton(tmpl['short_name'],
                                              callback_data=f"send_canned_{tmpl['id']}_{account_id}_{avito_chat_id}")])

    nav_buttons = []
    if page > 0: nav_buttons.append(
        InlineKeyboardButton("⬅️", callback_data=f"tmpl_list_{category_id}_{account_id}_{avito_chat_id}_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items: nav_buttons.append(
        InlineKeyboardButton("➡️", callback_data=f"tmpl_list_{category_id}_{account_id}_{avito_chat_id}_{page + 1}"))

    if nav_buttons: keyboard.append(nav_buttons)

    account = db.get_account_by_id(account_id)
    if account and account.get('default_category_id'):
        original_keyboard = query.message.reply_markup.inline_keyboard
        back_button_row = []
        for btn_row in original_keyboard:
            if any("manual_reply" in btn.callback_data for btn in btn_row):
                back_button_row = btn_row
                break
        if back_button_row:
            keyboard.append(back_button_row)
    else:
        keyboard.append(
            [InlineKeyboardButton("🔙 Назад к категориям", callback_data=f"cat_list_{account_id}_{avito_chat_id}_0")])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def send_canned_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("Отправляю ответ...")
    except BadRequest:
        pass
    parts = query.data.split('_')
    response_id = int(parts[2])
    account_id = int(parts[3])
    avito_chat_id = '_'.join(parts[4:])

    account = db.get_account_by_id(account_id)
    response_template = db.get_canned_response_by_id(response_id)

    if not account or not response_template:
        await query.message.reply_text("❌ Ошибка: не найден аккаунт или шаблон.")
        return

    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])
    if not token:
        await query.message.reply_text("❌ Ошибка авторизации Avito.")
        return

    try:
        await asyncio.to_thread(avito.send_message, token, account['profile_id'], avito_chat_id,
                                response_template['response_text'])
        db.log_message(account_id, avito_chat_id, 'out', 'canned', response_template['response_text'])
        await query.message.reply_text(f"✅ Ответ по шаблону «{response_template['short_name']}» успешно отправлен.")

        original_keyboard = _build_chat_interaction_keyboard(account, avito_chat_id)
        await query.edit_message_reply_markup(reply_markup=original_keyboard)

    except Exception as e:
        await query.message.reply_text(f"❌ Не удалось отправить ответ: {e}")
        logger.error(f"Ошибка отправки шаблонного ответа: {e}")


async def templates_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📋 Мои шаблоны", callback_data="templates_show_categories_0")],
        [InlineKeyboardButton("➕ Добавить шаблон", callback_data="add_template_start")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Управление шаблонами быстрых ответов:"

    await query.edit_message_text(text, reply_markup=reply_markup)
    return TEMPLATES_MENU

async def templates_show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])

    categories = db.get_categories()
    paginated_items, total_items = get_paginated_items(categories, page, 10)

    text = "🗂️ <b>Категории шаблонов</b>\n\nВыберите категорию для просмотра шаблонов:"
    keyboard = []
    if not paginated_items:
        text = "У вас еще нет категорий."

    for cat in paginated_items:
        keyboard.append([InlineKeyboardButton(cat['name'], callback_data=f"cat_view_{cat['id']}_0")])

    total_pages = (total_items + 9) // 10
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"templates_show_categories_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"templates_show_categories_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("➕ Добавить категорию", callback_data="add_category_start")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="templates_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return TEMPLATES_SHOW_CATEGORIES

async def templates_show_in_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    category_id = int(parts[2])
    page = int(parts[3])

    context.user_data['current_category_id'] = category_id
    category = next((c for c in db.get_categories() if c['id'] == category_id), None)
    if not category:
        await query.edit_message_text("❌ Категория не найдена.")
        return await templates_show_categories(update, context)

    templates = db.get_canned_responses_by_category(category_id)
    paginated_templates, total_items = get_paginated_items(templates, page, 10)

    text = f"<b>Шаблоны в категории «{html.escape(category['name'])}»</b>\n\nНажмите на шаблон для редактирования:"
    keyboard = [[InlineKeyboardButton("⚙️ Настройки категории", callback_data=f"cat_settings_{category_id}")]]

    if not paginated_templates:
        text = f"В категории «{html.escape(category['name'])}» пока нет шаблонов."
    else:
        for t in paginated_templates:
            keyboard.append([InlineKeyboardButton(f"  - {t['short_name']}", callback_data=f"template_edit_menu_{t['id']}")])

    total_pages = (total_items + 9) // 10
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"cat_view_{category_id}_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"cat_view_{category_id}_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data="templates_show_categories_0")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return TEMPLATES_SHOW_IN_CATEGORY

async def templates_category_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split('_')[-1])
    context.user_data['current_category_id'] = category_id
    category = next((c for c in db.get_categories() if c['id'] == category_id), None)

    text = f"Настройки категории «{html.escape(category['name'])}»"
    keyboard = [
        [InlineKeyboardButton("✍️ Переименовать", callback_data=f"cat_rename_{category_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"cat_delete_{category_id}")],
        [InlineKeyboardButton("⬅️ Назад к шаблонам", callback_data=f"cat_view_{category_id}_0")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return TEMPLATES_CATEGORY_SETTINGS


async def templates_category_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category_id = int(query.data.split('_')[-1])

    db.delete_category(category_id)
    await query.answer("Категория удалена. Шаблоны из нее теперь 'Без категории'.", show_alert=True)

    await _send_templates_show_categories_menu(
        chat_id=query.message.chat_id,
        context=context,
        message_id=query.message.message_id
    )

    return TEMPLATES_SHOW_CATEGORIES


async def templates_category_rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split('_')[-1])
    context.user_data['current_category_id'] = category_id
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите новое название для категории:",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_CATEGORY_RENAME


async def templates_category_rename_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    category_id = context.user_data['current_category_id']
    try:
        with sqlite3.connect(db.DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE response_categories SET name = ? WHERE id = ?", (new_name, category_id))
            conn.commit()
        await update.message.reply_text("✅ Имя категории обновлено.", reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Категория с таким именем уже существует.",
                                        reply_markup=ReplyKeyboardRemove())

    context.user_data.pop('current_category_id', None)
    await _send_templates_show_categories_menu(update.effective_chat.id, context)
    return TEMPLATES_SHOW_CATEGORIES


async def add_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите название для новой категории:",
        reply_markup=CANCEL_KEYBOARD
    )
    return ADD_CATEGORY_NAME

async def templates_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    template_id = int(query.data.split('_')[-1])
    context.user_data['template_id_to_edit'] = template_id
    template = db.get_canned_response_by_id(template_id)

    if not template:
        await query.edit_message_text("❌ Шаблон не найден.")
        return await templates_show_categories(update, context)

    text = (f"<b>Управление шаблоном «{html.escape(template['short_name'])}»</b>\n\n"
            f"<i>Текст:</i>\n<pre>{html.escape(template['response_text'])}</pre>")

    category_id = template.get('category_id')
    if not category_id:
        back_button = InlineKeyboardButton("⬅️ Назад к категориям", callback_data="templates_show_categories_0")
    else:
        back_button = InlineKeyboardButton("⬅️ Назад к шаблонам", callback_data=f"cat_view_{category_id}_0")

    keyboard = [
        [InlineKeyboardButton("✍️ Изменить текст", callback_data="template_edit_text")],
        [InlineKeyboardButton("🏷️ Изменить имя", callback_data="template_edit_name")],
        [InlineKeyboardButton("❌ Удалить этот шаблон", callback_data=f"template_delete_{template_id}")],
        [back_button]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return TEMPLATES_EDIT_MENU


async def templates_my_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])

    templates = db.get_canned_responses()
    paginated_templates, total_items = get_paginated_items(templates, page, 10)

    text = "<b>📝 Мои шаблоны</b>\n\nНажмите на шаблон, чтобы его отредактировать:"
    keyboard = []
    if not paginated_templates:
        text = "У вас еще нет добавленных шаблонов."
    else:
        current_category = None
        for t in paginated_templates:
            category_name = t.get('category_name') or 'Без категории'
            if category_name != current_category:
                keyboard.append([InlineKeyboardButton(f"🗂️ {html.escape(category_name)}", callback_data="ignore")])
                current_category = category_name
            keyboard.append(
                [InlineKeyboardButton(f"  - {t['short_name']}", callback_data=f"template_edit_menu_{t['id']}")])

    total_pages = (total_items + 10 - 1) // 10
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"templates_my_list_{page - 1}"))
    if page < total_pages - 1: nav_buttons.append(
        InlineKeyboardButton("➡️", callback_data=f"templates_my_list_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="templates_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return TEMPLATES_MY_LIST


async def templates_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Отправьте новое короткое имя для этого шаблона:",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_TEMPLATE_NEW_NAME


async def templates_edit_name_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    template_id = context.user_data['template_id_to_edit']
    try:
        db.update_canned_response(template_id, 'short_name', new_name)
        await update.message.reply_text("✅ Имя шаблона обновлено.", reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Шаблон с таким именем уже существует. Попробуйте другое.",
                                        reply_markup=ReplyKeyboardRemove())

    await _send_template_edit_menu(update.effective_chat.id, context)
    return TEMPLATES_EDIT_MENU


async def templates_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    template_id = int(query.data.split('_')[-1])

    template = db.get_canned_response_by_id(template_id)
    category_id = template.get('category_id') if template else None

    db.delete_canned_response(template_id)
    await query.answer("Шаблон удален", show_alert=True)
    context.user_data.pop('template_id_to_edit', None)

    if not category_id:
        await _send_templates_show_categories_menu(query.message.chat_id, context, query.message.message_id)
        return TEMPLATES_SHOW_CATEGORIES

    category = next((c for c in db.get_categories() if c['id'] == category_id), None)
    if not category:
        await _send_templates_show_categories_menu(query.message.chat_id, context, query.message.message_id)
        return TEMPLATES_SHOW_CATEGORIES

    templates = db.get_canned_responses_by_category(category_id)
    paginated_templates, total_items = get_paginated_items(templates, 0, 10)

    text = f"<b>Шаблоны в категории «{html.escape(category['name'])}»</b>\n\nНажмите на шаблон для редактирования:"
    keyboard = [[InlineKeyboardButton("⚙️ Настройки категории", callback_data=f"cat_settings_{category_id}")]]

    if not paginated_templates:
        text = f"В категории «{html.escape(category['name'])}» пока нет шаблонов."
    else:
        for t in paginated_templates:
            keyboard.append([InlineKeyboardButton(f"  - {t['short_name']}", callback_data=f"template_edit_menu_{t['id']}")])

    total_pages = (total_items + 9) // 10
    nav_buttons = []
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"cat_view_{category_id}_1"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data="templates_show_categories_0")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    return TEMPLATES_SHOW_IN_CATEGORY


async def templates_edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Отправьте новый текст для этого шаблона:",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_TEMPLATE_NEW_TEXT


async def templates_edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_text = update.message.text
    template_id = context.user_data['template_id_to_edit']
    db.update_canned_response(template_id, 'response_text', new_text)
    await update.message.reply_text("✅ Текст шаблона обновлен.", reply_markup=ReplyKeyboardRemove())

    await _send_template_edit_menu(update.effective_chat.id, context)
    return TEMPLATES_EDIT_MENU


async def _send_template_edit_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    template_id = context.user_data.get('template_id_to_edit')
    if not template_id: return

    template = db.get_canned_response_by_id(template_id)
    if not template:
        await context.bot.send_message(chat_id, "❌ Шаблон не найден.")
        return

    text = (f"<b>Управление шаблоном «{html.escape(template['short_name'])}»</b>\n\n"
            f"<i>Текст:</i>\n<pre>{html.escape(template['response_text'])}</pre>")

    category_id = template.get('category_id')
    if not category_id:
        back_button = InlineKeyboardButton("⬅️ Назад к категориям", callback_data="templates_show_categories_0")
    else:
        back_button = InlineKeyboardButton("⬅️ Назад к шаблонам", callback_data=f"cat_view_{category_id}_0")

    keyboard = [
        [InlineKeyboardButton("✍️ Изменить текст", callback_data="template_edit_text")],
        [InlineKeyboardButton("🏷️ Изменить имя", callback_data="template_edit_name")],
        [InlineKeyboardButton("❌ Удалить этот шаблон", callback_data=f"template_delete_{template_id}")],
        [back_button]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def add_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    categories = db.get_categories()
    if not categories:
        try:
            await query.answer("Сначала создайте хотя бы одну категорию!", show_alert=True)
        except BadRequest:
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text="Сначала создайте хотя бы одну категорию!")
        return TEMPLATES_MENU

    keyboard = [[InlineKeyboardButton(c['name'], callback_data=f"select_cat_{c['id']}")] for c in categories]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="templates_menu")])
    await query.edit_message_text("Выберите категорию для нового шаблона:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TEMPLATE_SELECT_CATEGORY


async def add_template_select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    context.user_data['category_id'] = int(query.data.split('_')[-1])

    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите короткое имя-идентификатор для шаблона (например, 'Приветствие'):",
        reply_markup=CANCEL_KEYBOARD
    )
    return ADD_TEMPLATE_NAME


async def add_category_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        db.add_category(update.message.text.strip())
        await update.message.reply_text(f"✅ Категория «{html.escape(update.message.text.strip())}» добавлена!",
                                        reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("Категория с таким именем уже существует.", reply_markup=ReplyKeyboardRemove())

    await _send_templates_show_categories_menu(update.effective_chat.id, context)
    return TEMPLATES_SHOW_CATEGORIES

async def add_template_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['short_name'] = update.message.text
    await update.message.reply_text("Теперь введите полный текст ответа для этого шаблона:",
                                    reply_markup=CANCEL_KEYBOARD)
    return ADD_TEMPLATE_TEXT

async def add_template_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['text'] = update.message.text
        db.add_canned_response(context.user_data['short_name'], context.user_data['text'],
                               context.user_data['category_id'])
        await update.message.reply_text(f"✅ Шаблон «{html.escape(context.user_data['short_name'])}» добавлен!",
                                        reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("Шаблон с таким именем уже существует. Попробуйте другое имя.",
                                        reply_markup=ReplyKeyboardRemove())

    context.user_data.clear()
    await _send_templates_main_menu(update.effective_chat.id, context)
    return TEMPLATES_MENU


async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    keyboard = [
        [InlineKeyboardButton("📊 За день", callback_data="stats_show_day")],
        [InlineKeyboardButton("📊 За неделю", callback_data="stats_show_week")],
        [InlineKeyboardButton("📊 За месяц", callback_data="stats_show_month")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    await query.edit_message_text("Выберите период для просмотра статистики:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return STATS_MENU


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    period = query.data.split('_')[-1]

    try:
        await query.answer()
    except BadRequest:
        pass

    await query.edit_message_text("⏳ Собираю статистику...")

    all_logs = await asyncio.to_thread(db.get_stats_for_period, period)

    total_in = sum(1 for log in all_logs if log.get('direction') == 'in')
    total_out = sum(1 for log in all_logs if log.get('direction') == 'out')

    period_map = {'day': 'день', 'week': 'неделю', 'month': 'месяц'}
    text = (f"<b>📊 Статистика за последний {period_map.get(period, '')}</b>\n\n"
            f"📥 Получено сообщений: <b>{total_in}</b>\n"
            f"📤 Отправлено ответов: <b>{total_out}</b>\n\n"
            f"Подробный отчет можно выгрузить в Excel.")

    keyboard = [
        [InlineKeyboardButton("📤 Выгрузить в .xlsx", callback_data=f"export_excel_{period}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="stats_menu")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return SHOW_STATS


async def export_stats_to_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    period = query.data.split('_')[-1]
    chat_id = query.message.chat_id

    all_logs = db.get_stats_for_period(period)

    if not all_logs:
        try:
            await query.answer("❌ Нет данных для экспорта за выбранный период.", show_alert=True)
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id,
                                           text="❌ Нет данных для экспорта за выбранный период.")
        return SHOW_STATS

    try:
        await query.answer("⏳ Готовлю Excel-файл...")
    except BadRequest:
        pass

    try:
        await query.message.delete()
    except BadRequest as e:
        logger.warning(f"Не удалось удалить сообщение с меню статистики: {e}")

    df = pd.DataFrame(all_logs)
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Europe/Moscow').dt.strftime('%d.%m.%Y %H:%M:%S')
    df = df[['timestamp', 'account_name', 'direction', 'reply_type', 'message_text']]
    df.columns = ['Дата (МСК)', 'Аккаунт Avito', 'Направление', 'Тип ответа', 'Текст сообщения']

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Статистика')
        worksheet = writer.sheets['Статистика']
        for i, col in enumerate(df.columns):
            column_len = max(df[col].astype(str).map(len).max(), len(col)) + 3
            worksheet.column_dimensions[get_column_letter(i + 1)].width = column_len
    output.seek(0)

    file_name = f"avito_stats_{period}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

    await context.bot.send_document(
        chat_id=chat_id, document=output, filename=file_name,
        caption=f"📊 Ваш отчет по статистике готов."
    )

    keyboard = [
        [InlineKeyboardButton("📊 За день", callback_data="stats_show_day")],
        [InlineKeyboardButton("📊 За неделю", callback_data="stats_show_week")],
        [InlineKeyboardButton("📊 За месяц", callback_data="stats_show_month")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выберите период для просмотра статистики:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return STATS_MENU


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass

    active_accounts = db.get_accounts(active_only=True)
    if not active_accounts:
        await query.edit_message_text("У вас нет активных аккаунтов для поиска.",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]]))
        return MAIN_MENU

    if len(active_accounts) == 1:
        context.user_data['search_account_id'] = active_accounts[0]['id']
        await query.message.delete()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Что ищем? Введите имя клиента, название объявления или текст сообщения:",
            reply_markup=CANCEL_KEYBOARD
        )
        return SEARCH_AWAIT_QUERY

    keyboard = []
    for acc in active_accounts:
        keyboard.append([InlineKeyboardButton(acc['name'], callback_data=f"search_acc_{acc['id']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Отмена", callback_data="main_menu")])
    await query.edit_message_text("Выберите аккаунт для поиска:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SEARCH_SELECT_ACCOUNT


async def search_select_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass

    account_id = int(query.data.split('_')[-1])
    context.user_data['search_account_id'] = account_id
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Что ищем? Введите имя клиента, название объявления или текст сообщения:",
        reply_markup=CANCEL_KEYBOARD
    )
    return SEARCH_AWAIT_QUERY


async def search_process_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.lower()
    account_id = context.user_data['search_account_id']
    await update.message.reply_text("⏳ Выполняю поиск (это может занять время)...", reply_markup=ReplyKeyboardRemove())

    account = db.get_account_by_id(account_id)
    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])

    if not token:
        await update.message.reply_text("❌ Ошибка авторизации Avito. Не удалось выполнить поиск.")
        return await start(update, context)

    active_period_days = int(context.bot_data['config']['SETTINGS'].get('ACTIVE_PERIOD_DAYS', 30))
    archive_boundary_ts = int(time.time()) - (active_period_days * 24 * 60 * 60)

    recent_chats = []
    offset = 0
    limit = 50
    stop_fetching = False

    while not stop_fetching:
        try:
            chats_batch = await asyncio.to_thread(avito.get_chats, token, account['profile_id'], limit, offset)
            if not chats_batch:
                break

            for chat in chats_batch:
                last_message_ts = chat.get('last_message', {}).get('created', 0)
                if last_message_ts < archive_boundary_ts:
                    stop_fetching = True
                    break
                recent_chats.append(chat)

            if len(chats_batch) < limit:
                break
            offset += limit
        except Exception as e:
            logger.error(f"Ошибка получения списка чатов для поиска: {e}")
            break

    found_chats = []
    found_chat_ids = set()

    for chat in recent_chats:
        if chat['id'] in found_chat_ids:
            continue

        context_title = chat.get('context', {}).get('value', {}).get('title', '').lower()
        last_message_text = chat.get('last_message', {}).get('content', {}).get('text', '').lower()
        user_name = chat.get('users', [{}])[0].get('name', '').lower()

        if query_text in context_title or query_text in last_message_text or query_text in user_name:
            found_chats.append(chat)
            found_chat_ids.add(chat['id'])
            continue

        try:
            await asyncio.sleep(0.1)
            messages = await asyncio.to_thread(avito.get_messages, token, account['profile_id'], chat['id'])
            for message in messages:
                message_text = message.get('content', {}).get('text', '').lower()
                if query_text in message_text:
                    found_chats.append(chat)
                    found_chat_ids.add(chat['id'])
                    break
        except Exception as e:
            logger.warning(f"Не удалось выполнить глубокий поиск для чата {chat['id']}: {e}")
            continue

    context.user_data['search_results'] = found_chats
    context.user_data['search_account'] = account

    if not found_chats:
        await update.message.reply_text("Ничего не найдено.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]]))
        context.user_data.clear()
        return SEARCH_SHOW_RESULTS

    context.user_data['from_search'] = True
    return await search_show_results(update, context)


async def search_show_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except BadRequest:
            pass
        page = int(query.data.split('_')[-1])
        message_to_edit = query.message
    else:
        page = 0
        message_to_edit = None

    results = context.user_data.get('search_results', [])
    paginated_results, total_items = get_paginated_items(results, page)

    text = f"Найдено чатов: {total_items}. Страница {page + 1}"
    keyboard = []
    for chat in paginated_results:
        title = chat.get('context', {}).get('value', {}).get('title', 'Без названия')
        last_msg_text = chat.get('last_message', {}).get('content', {}).get('text', 'Нет сообщений')
        btn_text = f"{title[:30]} | {last_msg_text[:30]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"search_select_chat_{chat['id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"search_page_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"search_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")])

    if message_to_edit:
        await message_to_edit.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    return SEARCH_SHOW_RESULTS


async def search_select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass

    chat_id_avito = query.data.split('_')[-1]
    account = context.user_data['search_account']
    results = context.user_data['search_results']

    selected_chat = next((chat for chat in results if chat['id'] == chat_id_avito), None)

    if not selected_chat:
        await query.edit_message_text("❌ Ошибка: чат не найден.")
        return MAIN_MENU

    title = selected_chat.get('context', {}).get('value', {}).get('title', 'Без названия')
    text = f"Выбран чат: <b>{html.escape(title)}</b>\n\nТеперь вы можете взаимодействовать с ним:"
    reply_markup = _build_chat_interaction_keyboard(account, chat_id_avito)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    context.user_data.clear()
    return MAIN_MENU


async def ai_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except BadRequest:
            pass

    settings = load_json(AI_SETTINGS_FILE, {})
    global_delay = settings.get('global_ai_reply_delay', 1)

    keyboard = [
        [InlineKeyboardButton("📜 Управление промптами", callback_data="ai_prompts_menu")],
        [InlineKeyboardButton("🔑 API Ключи провайдеров", callback_data="ai_keys_menu")],
        [InlineKeyboardButton(f"🕒 Глобальная задержка ({global_delay} мин.)", callback_data="set_global_ai_delay")],
        [InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="main_menu")]
    ]
    text = "⚙️ <b>Настройки AI</b>\n\nВыберите раздел для управления:"
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return AI_MENU


async def set_global_ai_delay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите глобальную задержку ответа ИИ в минутах (целое число, мин. 1).",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_GLOBAL_AI_DELAY


async def save_global_ai_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if delay < 1:
            raise ValueError
        settings = load_json(AI_SETTINGS_FILE, {})
        settings['global_ai_reply_delay'] = delay
        save_json(AI_SETTINGS_FILE, settings)
        await update.message.reply_text(f"✅ Глобальная задержка установлена на {delay} мин.",
                                        reply_markup=ReplyKeyboardRemove())
    except(ValueError, TypeError):
        await update.message.reply_text("Неверное значение. Введите целое число больше 0.",
                                        reply_markup=CANCEL_KEYBOARD)
        return AWAITING_GLOBAL_AI_DELAY

    return await ai_settings_menu(update, context)


async def ai_prompts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_ai_prompts_menu(query.message.chat_id, context, query.message.message_id)
    return AI_PROMPTS_MENU

async def _send_ai_prompts_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    keyboard = [
        [InlineKeyboardButton("📜 Мои промпты", callback_data="ai_my_prompts_0")],
        [InlineKeyboardButton("➕ Добавить промпт", callback_data="add_prompt_start")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ai_settings_menu")]
    ]
    text = "📜 <b>Управление промптами</b>\n\nВыберите действие:"
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info("Меню промптов не изменилось, пропуск редактирования.")
        else:
            logger.error(f"Ошибка при отправке меню промптов: {e}")

async def ai_my_prompts_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])

    prompts = db.get_prompts()
    paginated_prompts, total_items = get_paginated_items(prompts, page)

    text = "<b>📜 Мои промпты</b>\n\nНажмите на промпт, чтобы его отредактировать:"
    keyboard = []
    if not paginated_prompts:
        text = "У вас еще нет добавленных промптов."
    else:
        for p in paginated_prompts:
            keyboard.append([InlineKeyboardButton(p['name'], callback_data=f"prompt_edit_menu_{p['id']}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"ai_my_prompts_{page - 1}"))
    if (page + 1) * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"ai_my_prompts_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="ai_prompts_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return AI_MY_PROMPTS_LIST

async def ai_prompt_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prompt_id = int(query.data.split('_')[-1])
    context.user_data['prompt_id_to_edit'] = prompt_id
    await _send_ai_prompt_edit_menu(query.message.chat_id, context, query.message.message_id)
    return AI_PROMPTS_EDIT_MENU

async def _send_ai_prompt_edit_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    prompt_id = context.user_data.get('prompt_id_to_edit')
    if not prompt_id: return

    prompt = next((p for p in db.get_prompts() if p['id'] == prompt_id), None)
    if not prompt:
        await context.bot.send_message(chat_id, "❌ Промпт не найден.")
        return

    text = (f"<b>Управление промптом «{html.escape(prompt['name'])}»</b>\n\n"
            f"<i>Текст:</i>\n<pre>{html.escape(prompt['prompt_text'])}</pre>")

    keyboard = [
        [InlineKeyboardButton("✍️ Изменить текст", callback_data="prompt_edit_text")],
        [InlineKeyboardButton("🏷️ Изменить имя", callback_data="prompt_edit_name")],
        [InlineKeyboardButton("❌ Удалить этот промпт", callback_data=f"prompt_delete_{prompt_id}")],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data="ai_my_prompts_0")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML)

async def ai_prompt_edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Отправьте новый текст для этого промпта:",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_PROMPT_NEW_TEXT

async def ai_prompt_edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_text = update.message.text
    prompt_id = context.user_data['prompt_id_to_edit']
    db.update_prompt(prompt_id, 'prompt_text', new_text)
    await update.message.reply_text("✅ Текст промпта обновлен.", reply_markup=ReplyKeyboardRemove())
    await _send_ai_prompt_edit_menu(update.effective_chat.id, context)
    return AI_PROMPTS_EDIT_MENU

async def ai_prompt_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Отправьте новое короткое имя для этого промпта:",
        reply_markup=CANCEL_KEYBOARD
    )
    return AWAITING_PROMPT_NEW_NAME

async def ai_prompt_edit_name_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    prompt_id = context.user_data['prompt_id_to_edit']
    try:
        db.update_prompt(prompt_id, 'name', new_name)
        await update.message.reply_text("✅ Имя промпта обновлено.", reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Промпт с таким именем уже существует. Попробуйте другое.",
                                        reply_markup=ReplyKeyboardRemove())
    await _send_ai_prompt_edit_menu(update.effective_chat.id, context)
    return AI_PROMPTS_EDIT_MENU


async def ai_prompt_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prompt_id = int(query.data.split('_')[-1])
    db.delete_prompt(prompt_id)
    await query.answer("Промпт удален", show_alert=True)
    context.user_data.pop('prompt_id_to_edit', None)

    query.data = 'ai_my_prompts_0'
    return await ai_my_prompts_list(update, context)

async def add_prompt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите короткое название для нового промпта (например, 'Продажа машин'):",
        reply_markup=CANCEL_KEYBOARD
    )
    return AI_ADD_PROMPT_NAME


async def add_prompt_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['prompt_name'] = update.message.text
    await update.message.reply_text("Теперь введите полный текст промпта:", reply_markup=CANCEL_KEYBOARD)
    return AI_ADD_PROMPT_TEXT


async def add_prompt_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt_name = context.user_data['prompt_name']
    prompt_text = update.message.text
    try:
        db.add_prompt(prompt_name, prompt_text)
        await update.message.reply_text(f"✅ Промпт «{html.escape(prompt_name)}» успешно добавлен.",
                                        reply_markup=ReplyKeyboardRemove())
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Промпт с таким именем уже существует.", reply_markup=ReplyKeyboardRemove())

    context.user_data.clear()
    await _send_ai_prompts_menu(update.effective_chat.id, context)
    return AI_PROMPTS_MENU


async def edit_prompt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    prompt_id = int(query.data.split('_')[-1])
    prompt = next((p for p in db.get_prompts() if p['id'] == prompt_id), None)
    if not prompt:
        await query.edit_message_text("Промпт не найден.")
        return AI_PROMPTS_MENU

    context.user_data['prompt_id_to_edit'] = prompt_id
    text = (f"<b>Редактирование «{html.escape(prompt['name'])}»</b>\n\n"
            f"Текущий текст:\n<pre>{html.escape(prompt['prompt_text'])}</pre>\n\n"
            f"Отправьте новый текст для этого промпта:")

    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=CANCEL_KEYBOARD,
        parse_mode=ParseMode.HTML
    )
    return AWAITING_PROMPT_NEW_TEXT


async def edit_prompt_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt_id = context.user_data['prompt_id_to_edit']
    new_text = update.message.text
    db.update_prompt(prompt_id, new_text)
    await update.message.reply_text("✅ Промпт успешно изменен.", reply_markup=ReplyKeyboardRemove())

    context.user_data.pop('prompt_id_to_edit', None)

    class MockQuery:
        def __init__(self, message, data, chat_id):
            self.message, self.data = message, data
            self.message.chat_id = chat_id

        async def answer(self, *args, **kwargs): pass

        async def edit_message_text(self, *args, **kwargs):
            return await context.bot.send_message(self.message.chat_id, *args, **kwargs)

    mock_update = type('MockUpdate', (),
                       {'callback_query': MockQuery(update.message, 'ai_my_prompts_0', update.effective_chat.id)})
    return await ai_my_prompts_list(mock_update, context)


async def delete_prompt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    prompts = db.get_prompts()
    if not prompts:
        await query.edit_message_text("Нет промптов для удаления.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="ai_prompts_menu")]]))
        return AI_PROMPTS_MENU

    keyboard = []
    for p in prompts:
        keyboard.append([InlineKeyboardButton(f"❌ {p['name']}", callback_data=f"delete_prompt_confirm_{p['id']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="ai_prompts_menu")])
    await query.edit_message_text("Выберите промпт для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AI_DELETE_PROMPT_LIST


async def delete_prompt_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    prompt_id = int(query.data.split('_')[-1])
    db.delete_prompt(prompt_id)
    try:
        await query.answer("Промпт удален.", show_alert=True)
    except BadRequest:
        pass

    class MockQuery:
        def __init__(self, message, data, chat_id):
            self.message, self.data = message, data

        async def answer(self, *args, **kwargs): pass

        async def edit_message_text(self, *args, **kwargs):
            return await context.bot.send_message(self.message.chat.id, *args, **kwargs)

    mock_update = type('MockUpdate', (),
                       {'callback_query': MockQuery(query.message, 'ai_prompts_menu', update.effective_chat.id)})
    return await ai_prompts_menu(mock_update, context)


async def ai_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    settings = load_json(AI_SETTINGS_FILE, {})

    def get_key_status(provider):
        key = settings.get('api_keys', {}).get(provider)
        if not key: return "не задан"
        return f"<code>...{key[-4:]}</code>"

    text = (f"<b>🔑 Управление API ключами</b>\n\n"
            f"🔑 Gemini API: {get_key_status('gemini')}\n"
            f"🔑 OpenAI API: {get_key_status('openai')}\n"
            f"🔑 Deepseek API: {get_key_status('deepseek')}")

    keyboard = [
        [InlineKeyboardButton("🤖 Google Gemini", callback_data="set_api_key_gemini")],
        [InlineKeyboardButton("🧠 OpenAI GPT", callback_data="set_api_key_openai")],
        [InlineKeyboardButton("🌐 Deepseek", callback_data="set_api_key_deepseek")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ai_settings_menu")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return AI_KEYS_MENU


async def get_api_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data.split('_')[-1]
    context.user_data['provider'] = provider

    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Введите ваш API ключ для {provider.upper()}:",
        reply_markup=CANCEL_KEYBOARD
    )
    return GET_API_KEY


async def save_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data['provider']
    api_key = update.message.text.strip()
    settings = load_json(AI_SETTINGS_FILE, {"api_keys": {}})
    settings.setdefault('api_keys', {})[provider] = api_key
    save_json(AI_SETTINGS_FILE, settings)
    await update.message.reply_text(f"✅ API ключ для {provider.upper()} сохранен.", reply_markup=ReplyKeyboardRemove())

    context.user_data.pop('provider', None)
    await _send_ai_keys_menu(update.effective_chat.id, context)
    return AI_KEYS_MENU


async def _send_ai_keys_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    settings = load_json(AI_SETTINGS_FILE, {})

    def get_key_status(provider):
        key = settings.get('api_keys', {}).get(provider)
        if not key: return "не задан"
        return f"<code>...{key[-4:]}</code>"

    text = (f"<b>🔑 Управление API ключами</b>\n\n"
            f"🔑 Gemini API: {get_key_status('gemini')}\n"
            f"🔑 OpenAI API: {get_key_status('openai')}\n"
            f"🔑 Deepseek API: {get_key_status('deepseek')}")

    keyboard = [
        [InlineKeyboardButton("🤖 Google Gemini", callback_data="set_api_key_gemini")],
        [InlineKeyboardButton("🧠 OpenAI GPT", callback_data="set_api_key_openai")],
        [InlineKeyboardButton("🌐 Deepseek", callback_data="set_api_key_deepseek")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ai_settings_menu")]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def choose_provider_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    account_id = int(query.data.split('_')[-1])
    context.user_data['account_id'] = account_id

    keyboard = [
        [InlineKeyboardButton("🤖 Google Gemini", callback_data=f"set_provider_gemini")],
        [InlineKeyboardButton("🧠 OpenAI GPT", callback_data=f"set_provider_openai")],
        [InlineKeyboardButton("🌐 Deepseek", callback_data=f"set_provider_deepseek")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_{account_id}")]
    ]
    await query.edit_message_text("Выберите AI-провайдера для этого аккаунта:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_PROVIDER_FOR_ACCOUNT


async def set_provider_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    provider = query.data.split('_')[-1]
    account_id = context.user_data['account_id']
    db.update_account(account_id, 'ai_provider', provider)

    try:
        await query.answer(f"✅ Провайдер обновлен на {provider.upper()}", show_alert=True)
    except BadRequest:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=f"✅ Провайдер обновлен на {provider.upper()}.")

    await _send_automation_settings_menu(query.message.chat_id, context, query.message.message_id)
    return AUTOMATION_SETTINGS_MENU


async def ai_reply_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("🤖 Генерирую ответ...")
    except BadRequest:
        pass

    parts = query.data.split('_')
    account_id = int(parts[2])
    chat_id_avito = '_'.join(parts[3:])

    account = db.get_account_by_id(account_id)
    if not account:
        await query.message.reply_text("❌ Аккаунт не найден.")
        return

    settings = load_json(AI_SETTINGS_FILE, {})
    api_key = settings.get('api_keys', {}).get(account['ai_provider'])
    if not api_key:
        await query.message.reply_text(
            f"❌ API ключ для {account['ai_provider']} не найден. Укажите его в Настройках AI.")
        return

    token = await asyncio.to_thread(avito.get_token, account['client_id'], account['client_secret'])
    if not token:
        await query.message.reply_text("❌ Ошибка авторизации Avito.")
        return

    history = await asyncio.to_thread(avito.get_chat_history, token, account['profile_id'], chat_id_avito)

    prompt_text = account.get('prompt_text_full') or DEFAULT_PROMPT

    ai_response = await avito.generate_ai_reply(history, api_key, account['ai_provider'], prompt_text)

    if not ai_response or not ai_response.strip():
        await query.message.reply_text("❌ Не удалось сгенерировать ответ. ИИ вернул пустой результат.")
        return

    try:
        await asyncio.to_thread(avito.send_message, token, account['profile_id'], chat_id_avito, ai_response)

        await query.message.reply_text(
            f"✅ AI-ответ успешно отправлен:\n\n<i>{html.escape(ai_response)}</i>",
            parse_mode=ParseMode.HTML
        )

        db.log_message(account_id, chat_id_avito, 'out', 'ai_manual', ai_response)
    except Exception as e:
        await query.message.reply_text(f"❌ Не удалось отправить AI-ответ: {e}")
        logger.error(f"Ошибка отправки AI ответа: {e}")

async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
        await query.message.delete()
    except BadRequest as e:
        logger.info(f"Не удалось удалить сообщение (возможно, оно уже удалено): {e}")


async def ignore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass


async def hide_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_message(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account_id = context.user_data.get('account_id')

    keys_to_clear = [
        'editing_field', 'provider', 'prompt_id_to_edit', 'prompt_name',
        'prompt_type_to_set', 'short_name', 'text', 'category_id',
        'search_account_id', 'search_results', 'search_account'
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)

    if update.message:
        await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())

    if account_id:
        context.user_data['account_id'] = account_id
        await _send_account_menu(update.effective_chat.id, context)
        return EDIT_ACCOUNT_MENU

    return await start(update, context)


def main():
    db.init_database()
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        logger.critical(f"Файл конфигурации {CONFIG_FILE} не найден!")
        return
    config.read(CONFIG_FILE, encoding='utf-8')

    application = Application.builder().token(config['TELEGRAM']['BOT_TOKEN']).build()
    application.bot_data['config'] = config

    unified_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(start, pattern='^main_menu$'),
                CallbackQueryHandler(accounts_main_menu, pattern='^accounts_menu$'),
                CallbackQueryHandler(templates_main_menu, pattern='^templates_menu$'),
                CallbackQueryHandler(stats_menu, pattern='^stats_menu$'),
                CallbackQueryHandler(ai_settings_menu, pattern='^ai_settings_menu$'),
                CallbackQueryHandler(search_start, pattern='^search_start$'),
                CallbackQueryHandler(start_polling, pattern='^start_polling$'),
                CallbackQueryHandler(stop_polling, pattern='^stop_polling$'),
            ],
            ACCOUNTS_MENU: [
                CallbackQueryHandler(my_accounts_menu, pattern='^my_accounts$'),
                CallbackQueryHandler(add_account_start, pattern='^add_account_start$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            EDIT_ACCOUNT_LIST: [
                CallbackQueryHandler(edit_account_menu, pattern=r'^edit_'),
                CallbackQueryHandler(accounts_main_menu, pattern='^accounts_menu$'),
            ],
            EDIT_ACCOUNT_MENU: [
                CallbackQueryHandler(account_data_menu, pattern=r'^account_data_menu$'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu$'),
                CallbackQueryHandler(toggle_account_settings, pattern=r'^toggle_status_'),
                CallbackQueryHandler(my_accounts_menu, pattern='^my_accounts$'),
            ],
            ACCOUNT_DATA_MENU: [
                CallbackQueryHandler(edit_account_field_start, pattern=r'^edit_field_'),
                CallbackQueryHandler(delete_account_confirm, pattern=r'^delete_account_confirm_'),
                CallbackQueryHandler(edit_account_menu, pattern=r'^edit_'),
            ],
            DELETE_ACCOUNT_CONFIRM: [
                CallbackQueryHandler(delete_account_execute, pattern=r'^delete_account_execute_'),
                CallbackQueryHandler(account_data_menu, pattern=r'^account_data_menu$'),
            ],
            AUTOMATION_SETTINGS_MENU: [
                CallbackQueryHandler(choose_ai_mode, pattern=r'^choose_ai_mode_'),
                CallbackQueryHandler(set_ai_delay_start, pattern=r'^set_ai_delay_'),
                CallbackQueryHandler(choose_prompt_type_for_account, pattern=r'^choose_prompt_type_'),
                CallbackQueryHandler(choose_category_for_account, pattern=r'^choose_cat_acc_'),
                CallbackQueryHandler(choose_provider_for_account, pattern=r'^choose_provider_acc_'),
                CallbackQueryHandler(choose_autoreply_template, pattern=r'choose_autoreply_template_'),
                CallbackQueryHandler(edit_account_menu, pattern=r'^edit_'),
            ],
            CHOOSE_AI_MODE: [
                CallbackQueryHandler(set_ai_mode, pattern=r'^set_ai_mode_'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu$'),
            ],
            CHOOSE_PROMPT_TYPE_FOR_ACCOUNT: [
                CallbackQueryHandler(choose_prompt_for_account, pattern=r'^choose_prompt_for_'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu'),
            ],
            CHOOSE_PROMPT_FOR_ACCOUNT: [
                CallbackQueryHandler(set_prompt_for_account, pattern=r'^set_prompt_'),
                CallbackQueryHandler(choose_prompt_type_for_account, pattern=r'^choose_prompt_type_'),
            ],
            CHOOSE_CATEGORY_FOR_ACCOUNT: [
                CallbackQueryHandler(set_category_for_account, pattern=r'^set_cat_acc_'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu'),
            ],
            CHOOSE_AUTOREPLY_TEMPLATE: [
                CallbackQueryHandler(set_autoreply_template, pattern=r'^set_autoreply_template_'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu'),
            ],
            CHOOSE_PROVIDER_FOR_ACCOUNT: [
                CallbackQueryHandler(set_provider_for_account, pattern=r'^set_provider_'),
                CallbackQueryHandler(automation_settings_menu, pattern=r'^automation_settings_menu'),
            ],
            TEMPLATES_MENU: [
                CallbackQueryHandler(templates_show_categories, pattern=r'^templates_show_categories_'),
                CallbackQueryHandler(add_template_start, pattern=r'^add_template_start$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            TEMPLATES_SHOW_CATEGORIES: [
                CallbackQueryHandler(templates_show_in_category, pattern=r'^cat_view_'),
                CallbackQueryHandler(add_category_start, pattern=r'^add_category_start'),
                CallbackQueryHandler(templates_main_menu, pattern=r'^templates_menu'),
            ],
            TEMPLATES_SHOW_IN_CATEGORY: [
                CallbackQueryHandler(templates_edit_menu, pattern=r'^template_edit_menu_'),
                CallbackQueryHandler(templates_category_settings, pattern=r'^cat_settings_'),
                CallbackQueryHandler(templates_show_categories, pattern=r'^templates_show_categories_'),
            ],
            TEMPLATES_CATEGORY_SETTINGS: [
                CallbackQueryHandler(templates_category_rename_start, pattern=r'^cat_rename_'),
                CallbackQueryHandler(templates_category_delete, pattern=r'^cat_delete_'),
                CallbackQueryHandler(templates_show_in_category, pattern=r'^cat_view_'),
            ],
            TEMPLATES_EDIT_MENU: [
                CallbackQueryHandler(templates_edit_text_start, pattern=r'^template_edit_text$'),
                CallbackQueryHandler(templates_edit_name_start, pattern=r'^template_edit_name$'),
                CallbackQueryHandler(templates_delete_confirm, pattern=r'^template_delete_'),
                CallbackQueryHandler(templates_show_in_category, pattern=r'^cat_view_'),
            ],
            TEMPLATE_SELECT_CATEGORY: [
                CallbackQueryHandler(add_template_select_category, pattern=r'^select_cat_'),
                CallbackQueryHandler(templates_main_menu, pattern='^templates_menu$'),
            ],
            STATS_MENU: [
                CallbackQueryHandler(show_stats, pattern=r'^stats_show_'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            SHOW_STATS: [
                CallbackQueryHandler(export_stats_to_excel, pattern=r'^export_excel_'),
                CallbackQueryHandler(stats_menu, pattern='^stats_menu$'),
            ],
            AI_MENU: [
                CallbackQueryHandler(ai_prompts_menu, pattern='^ai_prompts_menu$'),
                CallbackQueryHandler(ai_keys_menu, pattern='^ai_keys_menu$'),
                CallbackQueryHandler(set_global_ai_delay_start, pattern=r'^set_global_ai_delay$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            AI_PROMPTS_MENU: [
                CallbackQueryHandler(ai_my_prompts_list, pattern=r'^ai_my_prompts_'),
                CallbackQueryHandler(add_prompt_start, pattern=r'^add_prompt_start$'),
                CallbackQueryHandler(ai_settings_menu, pattern='^ai_settings_menu$'),
            ],
            AI_MY_PROMPTS_LIST: [
                CallbackQueryHandler(ai_prompt_edit_menu, pattern=r'^prompt_edit_menu_'),
                CallbackQueryHandler(ai_my_prompts_list, pattern=r'^ai_my_prompts_'),
                CallbackQueryHandler(ai_prompts_menu, pattern=r'^ai_prompts_menu$'),
            ],
            AI_PROMPTS_EDIT_MENU: [
                CallbackQueryHandler(ai_prompt_edit_text_start, pattern=r'^prompt_edit_text$'),
                CallbackQueryHandler(ai_prompt_edit_name_start, pattern=r'^prompt_edit_name$'),
                CallbackQueryHandler(ai_prompt_delete_confirm, pattern=r'^prompt_delete_'),
                CallbackQueryHandler(ai_my_prompts_list, pattern=r'^ai_my_prompts_'),
            ],
            AI_KEYS_MENU: [
                CallbackQueryHandler(get_api_key_start, pattern=r'^set_api_key_'),
                CallbackQueryHandler(ai_settings_menu, pattern='^ai_settings_menu$'),
            ],
            SEARCH_SELECT_ACCOUNT: [
                CallbackQueryHandler(search_select_account, pattern=r'^search_acc_'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            SEARCH_SHOW_RESULTS: [
                CallbackQueryHandler(search_show_results, pattern=r'^search_page_'),
                CallbackQueryHandler(search_select_chat, pattern=r'^search_select_chat_'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            ADD_ACCOUNT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_get_name)],
            ADD_ACCOUNT_CLIENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_get_client_id)],
            ADD_ACCOUNT_CLIENT_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_get_client_secret)],
            ADD_ACCOUNT_PROFILE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_get_profile_id)],
            ADD_ACCOUNT_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_get_chat_id)],
            EDIT_ACCOUNT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_account_field)],
            SEARCH_AWAIT_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_process_query)],
            GET_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_key)],
            AWAITING_AI_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_ai_delay)],
            AWAITING_GLOBAL_AI_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_global_ai_delay)],
            AI_ADD_PROMPT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_prompt_get_name)],
            AI_ADD_PROMPT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_prompt_get_text)],
            AWAITING_PROMPT_NEW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_prompt_edit_text_save)],
            AWAITING_PROMPT_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_prompt_edit_name_save)],
            ADD_TEMPLATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_get_name)],
            ADD_TEMPLATE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_get_text)],
            ADD_CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category_get_name)],
            AWAITING_TEMPLATE_NEW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, templates_edit_text_save)],
            AWAITING_TEMPLATE_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, templates_edit_name_save)],
            AWAITING_CATEGORY_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, templates_category_rename_save)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
        allow_reentry=True
    )

    manual_reply_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(manual_reply_start, pattern=r'^manual_reply_')],
        states={
            AWAITING_MANUAL_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_reply_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False
    )

    application.add_handler(unified_conv_handler)
    application.add_handler(manual_reply_handler)
    application.add_handler(CallbackQueryHandler(ai_reply_process, pattern=r'^ai_reply_'))
    application.add_handler(CallbackQueryHandler(request_chat_history, pattern=r'^history_'))
    application.add_handler(CallbackQueryHandler(delete_message, pattern=r'^delete_message'))
    application.add_handler(CallbackQueryHandler(send_canned_response, pattern=r'^send_canned_'))
    application.add_handler(CallbackQueryHandler(canned_response_router, pattern=r'^canned_start_'))
    application.add_handler(CallbackQueryHandler(show_categories_for_reply, pattern=r'^cat_list_'))
    application.add_handler(CallbackQueryHandler(show_templates_for_reply, pattern=r'^tmpl_list_'))
    application.add_handler(CallbackQueryHandler(ignore_callback, pattern=r'^ignore'))
    application.add_handler(CallbackQueryHandler(hide_history, pattern=r'^hide_history'))
    application.add_handler(CallbackQueryHandler(restore_original_buttons, pattern=r'^restore_buttons_'))

    check_interval_str = config['SETTINGS'].get('CHECK_INTERVAL', '300')
    check_interval = int(check_interval_str) if check_interval_str.isdigit() else 300
    application.job_queue.run_repeating(check_avito_messages, interval=check_interval, first=5)

    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()