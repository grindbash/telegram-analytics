import sys
import io
import os
import asyncio
import json
import logging
import pytz
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory, current_app
from telethon.sync import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.errors import FloodWaitError, SessionPasswordNeededError, ChannelPrivateError
from telethon.tl.types import PeerChannel
from telethon.tl.functions.channels import GetFullChannelRequest
from dotenv import load_dotenv
import requests
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import base64
import time
import re
import hashlib
from flask import make_response
from urllib.parse import quote

# Глобальный кэш для хранения сгенерированных PDF (временное решение)
pdf_cache = {}
CACHE_EXPIRY = 300  # 5 минут

# Добавить в начале файла после импортов
try:
    pdfmetrics.registerFont(TTFont('DejaVuSans', 'static/fonts/DejaVuSans.ttf'))
    pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', 'static/fonts/DejaVuSans-Bold.ttf'))
    CYRILLIC_FONT_AVAILABLE = True
except:
    logger.warning("Шрифты DejaVuSans не найдены. Кириллица в PDF может отображаться некорректно.")
    CYRILLIC_FONT_AVAILABLE = False

def get_safe_filename(channel_info):
    """Создает безопасное имя файла на основе username канала"""
    try:
        # Получаем username из информации о канале
        username = channel_info.get('username', '')
        if username:
            # Если username начинается с @, убираем его
            if username.startswith('@'):
                username = username[1:]
            # Используем username как основу для имени файла
            safe_name = f"{username}_report.pdf"
        else:
            # Если username отсутствует, используем ID канала
            channel_id = channel_info.get('id', '')
            safe_name = f"channel_{channel_id}_report.pdf"
        
        # Заменяем все небезопасные символы
        safe_name = re.sub(r'[^\w\-_.]', '_', safe_name)
        
        return safe_name
    except:
        return 'telegram_report.pdf'

# Устанавливаем UTF-8 как стандартную кодировку
if sys.stdout.encoding != 'UTF-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', newline='', line_buffering=True)
    
if sys.stderr.encoding != 'UTF-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', newline='', line_buffering=True)

# Загружаем переменные окружения
load_dotenv()
# В начале файла (после импортов)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


# =============================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================
class SafeFileHandler(logging.FileHandler):
    """Обработчик логов с безопасной обработкой Unicode для Windows"""
    def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
        super().__init__(filename, mode, encoding, delay)
    
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                safe_msg = msg.encode('utf-8', 'backslashreplace').decode('utf-8')
                stream.write(safe_msg + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)
        except Exception:
            self.handleError(record)

os.makedirs('logs', exist_ok=True)
log_file = 'logs/app.log'

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

file_handler = SafeFileHandler(log_file, encoding='utf-8')
stream_handler = logging.StreamHandler()

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, stream_handler]
)
logger = logging.getLogger(__name__)
logger.info("Логирование настроено с поддержкой UTF-8")

# =============================================
# ОСТАЛЬНАЯ ЧАСТЬ ПРИЛОЖЕНИЯ
# =============================================

# Глобальный цикл событий
loop = None

def get_or_create_eventloop():
    """Получаем или создаем новый event loop"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Loop is closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

# Инициализация Flask
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # Для корректного отображения русского языка в JSON

# Создаем статическую папку если её нет
os.makedirs('static', exist_ok=True)

# Конфигурация
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
SESSION_PATH = os.getenv('TELEGRAM_SESSION_FILE', 'analytics_session.session')

# Конфигурация Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Заголовки для запросов к Supabase
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# Конфигурация OpenRouter
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = "deepseek/deepseek-v3.1-terminus"

class TelegramAnalytics:
    def __init__(self):
        self.client = None
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        self._loop = None

    def get_period_text(self, hours):
        """Получение текстового описания периода"""
        if hours == 24:
            return "24 часа"
        elif hours == 72:
            return "3 дня"
        elif hours == 168:
            return "7 дней"
        elif hours == 720:
            return "30 дней"
        else:
            return f"{hours} часов"

    def _format_content_type(self, content_type):
        """Форматирование названий типов контента"""
        type_mapping = {
            'mixed_media_with_text': 'текст + медиа',
            'text': 'текст',
            'photo': 'фото',
            'video': 'видео',
            'audio': 'аудио',
            'document': 'документ',
            'media': 'медиа',
            'media_album': 'медиа-альбом',
            'photo_with_text': 'фото + текст',
            'video_with_text': 'видео + текст'
        }
        return type_mapping.get(content_type, content_type)  
        
    def get_safe_filename(filename):
        """Создает безопасное имя файла для HTTP заголовков"""
        try:
            return quote(filename)
        except:
            try:
                return filename.encode('ascii', 'ignore').decode('ascii') or 'telegram_report.pdf'
            except:
                return 'telegram_report.pdf'
                
    def _get_loop(self):
        """Получаем текущий event loop"""
        if self._loop is None or self._loop.is_closed():
            self._loop = get_or_create_eventloop()
        return self._loop
    
    async def init_client(self):
        """Инициализация Telegram клиента"""
        try:
            # Проверяем существует ли файл сессии
            session_exists = os.path.exists(SESSION_PATH)
            
            session_str = os.getenv('TELEGRAM_SESSION_STRING')
            if session_str:
                from telethon.sessions import StringSession
                self.client = TelegramClient(
                    StringSession(session_str),
                    API_ID,
                    API_HASH
                )
                logger.info("Используется строковая сессия")
            else:
                self.client = TelegramClient(
                    SESSION_PATH, 
                    API_ID, 
                    API_HASH
                )
                logger.info("Используется файловая сессия")
            
            # Подключаемся к Telegram
            await self.client.connect()
            
            # Если сессия существует, проверяем авторизацию
            if session_exists:
                if not await self.client.is_user_authorized():
                    logger.warning("Сессия устарела. Требуется новая авторизация.")
                    await self.client.start()
            else:
                # Новая сессия - запускаем процесс авторизации
                await self.client.start()
            
            # Проверяем тип аккаунта
            me = await self.client.get_me()
            if me.bot:
                logger.error("ОШИБКА: Используется бот-аккаунт! Нужен пользовательский аккаунт")
                return False
            
            logger.info("Telegram клиент инициализирован успешно")
            logger.info(f"Авторизован как: {me.first_name} ({me.phone})")
            return True
        except Exception as e:
            logger.error(f"Ошибка инициализации клиента: {str(e)}", exc_info=True)
            return False
    
    async def get_channel_info(self, channel_identifier):
        """Получение информации о канале по username или ID"""
        try:
            # Определяем тип идентификатора
            if isinstance(channel_identifier, int) or (isinstance(channel_identifier, str) and channel_identifier.startswith('-100')):
                entity = await self.client.get_entity(PeerChannel(int(channel_identifier)))
            else:
                entity = await self.client.get_entity(channel_identifier)
            
            # Пытаемся получить расширенную информацию о канале
            subscribers = 0
            try:
                full_channel = await self.client(GetFullChannelRequest(channel=entity))
                subscribers = full_channel.full_chat.participants_count
                logger.info(f"Получена расширенная информация о канале: {subscribers} подписчиков")
            except Exception as e:
                logger.warning(f"Не удалось получить полную информацию о канале: {str(e)}")
                # Пробуем получить из базовой информации
                subscribers = getattr(entity, 'participants_count', 0)
            
            return {
                'id': entity.id,
                'title': entity.title,
                'username': entity.username,
                'subscribers': subscribers,
                'description': getattr(entity, 'about', '')
            }
        except ValueError:
            logger.error(f"Канал '{channel_identifier}' не найден")
            return None
        except ChannelPrivateError:
            logger.error(f"Приватный канал: {channel_identifier}. Требуется подписка")
            return {
                'error': 'Приватный канал',
                'message': 'Требуется подписка на канал'
            }
        except Exception as e:
            logger.error(f"Ошибка получения информации о канале: {str(e)}", exc_info=True)
            return None

    # Добавляем метод получения истории постов
    async def get_channel_history(self, channel_identifier, limit=30):
        """Получение истории текстовых постов из канала"""
        try:
            if not self.client or not self.client.is_connected():
                if not await self.init_client():
                    return {'error': 'Не удалось подключиться к Telegram'}
            
            # Получаем информацию о канале
            channel_info = await self.get_channel_info(channel_identifier)
            if not channel_info or 'error' in channel_info:
                return {
                    'error': 'Канал не найден или приватный',
                    'details': channel_info.get('message', 'Убедитесь что вы подписаны на канал')
                }
            
            # Получаем сообщения
            all_messages = []
            try:
                # Получаем больше сообщений, так как будем фильтровать только текстовые
                all_messages = await self.client.get_messages(
                    channel_identifier, 
                    limit=min(limit * 2, 100)  # Берем в 2 раза больше для фильтрации
                )
            except Exception as e:
                logger.error(f"Ошибка получения сообщений: {str(e)}", exc_info=True)
                return {'error': f'Ошибка получения сообщений: {str(e)}'}
            
            # Фильтруем текстовые посты
            text_posts = []
            for msg in all_messages:
                if len(text_posts) >= limit:
                    break
                    
                if msg.text and msg.text.strip():  # Только сообщения с текстом
                    moscow_time = msg.date.replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                    
                    text_posts.append({
                        'id': msg.id,
                        'date': moscow_time.strftime('%Y-%m-%d %H:%M'),
                        'text': msg.text,
                        'views': self._get_views(msg),
                        'reactions': self._get_reactions(msg),
                        'forwards': self._get_forwards(msg),
                        'comments': self._get_comments(msg)
                    })
            
            return {
                'channel_info': channel_info,
                'posts': text_posts,
                'total_count': len(text_posts),
                'requested_limit': limit
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения истории: {str(e)}", exc_info=True)
            return {'error': f'Ошибка получения истории: {str(e)}'}
    
    async def generate_ai_analysis(self, report_data):
        """Генерация ИИ анализа через OpenRouter с поддержкой fallback режима"""
        try:
            hours_back = report_data['analysis_period']['hours_back']
            used_fallback = report_data['analysis_period'].get('used_fallback', False)
            actual_period = report_data['analysis_period'].get('actual_period', '')
            
            # Формируем описание периода с учетом fallback
            if used_fallback:
                period_text = f"анализ последних 30 постов (канал неактивен, {report_data['analysis_period'].get('fallback_reason', 'последний пост более 30 дней назад')})"
            else:
                period_text = self.get_period_text(hours_back)
                
            prompt = f"""
            Ты эксперт по анализу Telegram каналов с опытом в data-driven маркетинге. Проанализируй предоставленные данные за период: {period_text} и дай развернутые рекомендации.
            
            {'⚠️ ВНИМАНИЕ: Этот канал неактивен в течение длительного времени. Проанализируй исторические данные и дай рекомендации по возобновлению активности.' if used_fallback else ''}

            Ты не описываешь процесс мышления.
            Ты сразу выдаёшь готовый, структурированный отчёт на основе данных.
            Не используй фразы вроде 'начну с', 'теперь проверю', 'я думаю'.
            Начни ответ с пункта '1. Краткое резюме по каналу'.
            Ответ должен быть профессиональным, полным и без 'воды'.

            Контекст:
            - Канал: {report_data['channel_info']['title']}
            - Подписчиков: {report_data['channel_info']['subscribers']}
            - Период анализа: {period_text}

            Данные для анализа:
            {json.dumps(report_data['summary'], indent=2, ensure_ascii=False)}

            Требования к анализу:

            1. Ключевые тенденции:
            - Проанализируй динамику роста/падения подписчиков
            - Выяви закономерности в активности аудитории
            - Определи аномалии в статистике (резкие скачки или падения)

            2. Рекомендации по контенту:
            - Определи наиболее эффективные форматы контента (текст, видео, опросы и т.д.)
            - Проанализируй темы с максимальной вовлеченностью
            - Предложи оптимальное соотношение типов контента
            - Дай рекомендации по улучшению контент-стратегии

            3. Оптимальное время публикаций:
            - Определи часы и дни максимальной активности аудитории
            - Предложи конкретное расписание публикаций
            - Дай рекомендации по частоте публикаций

            4. Оценка вовлеченности:
            - Рассчитай Engagement Rate (ER) по формуле: (Реакции + Комментарии + Репосты) / Подписчики * 100%
            - Сравни показатели с бенчмарками для ниши
            - Проанализируй CTR и другие метрики вовлеченности
            - Выяви посты с аномально высокой/низкой вовлеченностью

            5. Прогноз роста:
            - На основе текущих метрик построй прогноз на 7/30 дней
            - Оцени потенциал вирального роста
            - Дай рекомендации по привлечению новой аудитории

            {'6. Рекомендации по возобновлению активности:' if used_fallback else ''}
            {'- Проанализируй потенциал возобновления канала' if used_fallback else ''}
            {'- Предложи стратегию возврата аудитории' if used_fallback else ''}
            {'- Оцени риски и возможности' if used_fallback else ''}

            Дополнительно:
            - Дай рекомендации по SEO в Telegram
            - Предложи инструменты для автоматизации аналитики

            Формат вывода:
            1. Краткое резюме по каналу
            2. Детальный анализ по каждому пункту, но кратко и по факту изложи его
            3. Конкретные рекомендации для внедрения
            4. Прогноз развития на ближайший период
            {'' if used_fallback else '5. Рекомендации по возобновлению активности (если канал неактивен)'}
            """
            
            # Логируем длину промпта
            logger.info(f"Длина промпта для ИИ: {len(prompt)} символов")
            
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://your-domain.com",
                "X-Title": "Telegram Analytics"
            }
            
            payload = {
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "Ты эксперт по анализу Telegram каналов с опытом в data-driven маркетинге. Проанализируй предоставленные данные и дай развернутые рекомендации."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.5,
                "max_tokens": 16000,
                "repetition_penalty": 1.05,
                "stream": False
            }
            
            logger.info(f"Отправка запроса к OpenRouter: {OPENROUTER_API_URL}")
            
            # Синхронный запрос
            response = requests.post(
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
                timeout=120
            )
            
            # Детальное логирование ответа
            logger.info(f"Статус ответа OpenRouter: {response.status_code}")
            
            try:
                response_data = response.json()
                logger.info(f"Тело ответа (первые 500 символов): {str(response_data)[:500]}")
                
                # Проверяем, не был ли ответ обрезан
                finish_reason = response_data.get('choices', [{}])[0].get('finish_reason', '')
                if finish_reason == 'length':
                    logger.warning("Ответ ИИ был обрезан из-за ограничения длины токенов")
                    
            except json.JSONDecodeError:
                logger.error(f"Не удалось распарсить JSON: {response.text[:500]}")
                return "Ошибка: неверный формат ответа ИИ"
            
            # Проверяем различные форматы ответа
            if response.status_code != 200:
                error_msg = response_data.get('error', {}).get('message', response.text[:200])
                logger.error(f"OpenRouter API error: {response.status_code} - {error_msg}")
                return f"Ошибка API: {response.status_code} - {error_msg}"
            
            # Проверяем возможные форматы ответа
            if 'choices' in response_data and response_data['choices']:
                content = response_data['choices'][0]['message']['content']
                # Добавляем предупреждение, если ответ был обрезан
                if finish_reason == 'length':
                    content += "\n\n⚠️ Внимание: анализ был сокращен из-за ограничений длины. Для полного анализа используйте платные модели с большим контекстом."
                return content
            elif 'message' in response_data:
                return response_data['message']
            elif 'text' in response_data:
                return response_data['text']
            elif 'error' in response_data:
                return f"Ошибка ИИ: {response_data['error']}"
            else:
                logger.error(f"Неожиданный формат ответа: {json.dumps(response_data, indent=2)[:500]}")
                return "Ошибка: неверный формат ответа ИИ"
                
        except Exception as e:
            logger.error(f"Ошибка ИИ анализа: {str(e)}", exc_info=True)
            return f"Ошибка при генерации ИИ анализа: {str(e)}"
        finally:
            pass  # Добавляем блок finally для коррекции синтаксиса

    def _get_views(self, message):
        """Безопасное получение количества просмотров"""
        views = getattr(message, 'views', None)
        return views if views is not None else 0
    
    def _get_reactions(self, message):
        """Безопасное получение количества реакций"""
        if hasattr(message, 'reactions') and message.reactions:
            return sum(r.count for r in message.reactions.results)
        return 0
    
    def _get_forwards(self, message):
        """Безопасное получение количества пересылок"""
        forwards = getattr(message, 'forwards', None)
        if forwards is None:
            return 0
        return forwards
    
    def _get_comments(self, message):
        """Безопасное получение количества комментариев"""
        if hasattr(message, 'replies') and message.replies:
            return message.replies.replies
        return 0

    def _categorize_group_content(self, messages):
        """Улучшенная категоризация для смешанных альбомов"""
        content_types = set()
        text_present = False
        media_count = 0
        
        for msg in messages:
            # Проверяем наличие текста
            if msg.text and msg.text.strip():
                text_present = True
            
            # Определяем тип медиа
            if msg.media:
                media_count += 1
                if isinstance(msg.media, MessageMediaPhoto):
                    content_types.add('photo')
                elif isinstance(msg.media, MessageMediaDocument):
                    if msg.media.document:
                        mime_type = msg.media.document.mime_type
                        if mime_type.startswith('video/'):
                            content_types.add('video')
                        elif mime_type.startswith('audio/'):
                            content_types.add('audio')
                        else:
                            content_types.add('document')
                else:
                    content_types.add('media')
        
        # Определяем основной тип контента
        if 'video' in content_types:
            base_type = 'video'
        elif 'photo' in content_types:
            base_type = 'photo'
        elif 'audio' in content_types:
            base_type = 'audio'
        elif 'document' in content_types:
            base_type = 'document'
        else:
            base_type = 'media'
        
        # Формируем итоговый тип
        if media_count == 0:
            return 'text' if text_present else 'other'
        
        # Для смешанных медиа
        if len(content_types) > 1:
            media_type = 'mixed_media'
        else:
            media_type = base_type
        
        # Добавляем указание на текст
        if text_present:
            return f"{media_type}_with_text"
        
        return f"{media_type}_album"

    def _process_message_group(self, group_messages):
        """Обработка группы сообщений как единого поста"""
        if not group_messages:
            return None
            
        # Сортируем сообщения по ID (для согласованности)
        group_messages.sort(key=lambda m: m.id)
        main_message = group_messages[0]
        
        # Собираем метрики по всей группе
        group_views = self._get_views(main_message)  # Просмотры одинаковы для всех в группе
        group_reactions = sum(self._get_reactions(msg) for msg in group_messages)
        group_forwards = sum(self._get_forwards(msg) for msg in group_messages)
        group_comments = self._get_comments(main_message)  # Комментарии обычно к первому сообщению
        
        # Определяем тип контента
        content_type = self._categorize_group_content(group_messages)
        
        # Формируем текст превью
        text_preview = ""
        has_text = False
        for msg in group_messages:
            if msg.text and msg.text.strip():
                text_preview = (msg.text[:100] + '...') if len(msg.text) > 100 else msg.text
                has_text = True
                break
        
        if not has_text:
            # Формируем описание для медиа-альбома без текста
            media_types = self._get_media_types(group_messages)
            if media_types:
                text_preview = f"Альбом: {', '.join(media_types)}"
            else:
                text_preview = "Медиа контент без описания"
        
        return {
            'id': main_message.id,
            'date': main_message.date,
            'views': group_views,
            'reactions': group_reactions,
            'forwards': group_forwards,
            'comments': group_comments,
            'text_preview': text_preview,
            'content_type': content_type,
            'is_group': True,
            'group_size': len(group_messages)
        }

    def _get_media_types(self, messages):
        """Возвращает типы медиа в группе для описания"""
        media_types = []
        for msg in messages:
            if not msg.media:
                continue
                
            if isinstance(msg.media, MessageMediaPhoto):
                media_types.append('фото')
            elif isinstance(msg.media, MessageMediaDocument):
                if msg.media.document:
                    mime_type = msg.media.document.mime_type
                    if mime_type.startswith('video/'):
                        media_types.append('видео')
                    elif mime_type.startswith('audio/'):
                        media_types.append('аудио')
                    else:
                        media_types.append('документ')
            else:
                media_types.append('медиа')
        
        # Убираем дубликаты
        return list(set(media_types))
    
    async def analyze_channel(self, channel_identifier, hours_back=24):
        """Основной метод анализа канала с автоматическим fallback на последние 30 постов"""
        try:
            # Проверяем подключение клиента
            if not self.client or not self.client.is_connected():
                if not await self.init_client():
                    return {'error': 'Не удалось подключиться к Telegram'}
            
            # Получаем информацию о канале
            channel_info = await self.get_channel_info(channel_identifier)
            if not channel_info or 'error' in channel_info:
                return {
                    'error': 'Канал не найден или приватный',
                    'details': channel_info.get('message', 'Убедитесь что вы подписаны на канал')
                }
            
            # Временной диапазон для запрошенного периода
            end_time = datetime.now(self.moscow_tz)
            start_time = end_time - timedelta(hours=hours_back)
            
            logger.info(f"Анализ канала: {channel_info['title']}")
            logger.info(f"Текущее время сервера: {datetime.now(self.moscow_tz)}")
            logger.info(f"Диапазон анализа: {start_time} - {end_time}")
            
            # Получаем все доступные сообщения
            all_messages = []
            last_message_date = None
            try:
                all_messages = await self.client.get_messages(
                    channel_identifier, 
                    limit=1000
                )
                
                logger.info(f"Получено сообщений: {len(all_messages)}")
                
                # Определяем дату последнего поста
                if all_messages:
                    last_message_date = max(msg.date.replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz) 
                                          for msg in all_messages if msg.date)
                    logger.info(f"Последний пост: {last_message_date}")
                
            except ChannelPrivateError:
                return {
                    'error': 'Приватный канал',
                    'message': 'У вас нет доступа к этому каналу. Убедитесь что вы подписаны.'
                }
            except Exception as e:
                logger.error(f"Ошибка получения сообщений: {str(e)}", exc_info=True)
                return {'error': f'Ошибка получения сообщений: {str(e)}'}
            
            # Проверяем, когда был последний пост
            used_fallback = False
            fallback_reason = None
            actual_period_text = self.get_period_text(hours_back)
            
            if last_message_date:
                days_since_last_post = (datetime.now(self.moscow_tz) - last_message_date).days
                logger.info(f"Дней с последнего поста: {days_since_last_post}")
                
                # Если последний пост был больше 30 дней назад, используем fallback
                if days_since_last_post > 30:
                    used_fallback = True
                    fallback_reason = f"Последний пост был {days_since_last_post} дней назад"
                    logger.info(f"Используем fallback: {fallback_reason}")
            
            # Фильтруем сообщения по временному диапазону или используем fallback
            messages_to_process = []
            
            if used_fallback:
                # Fallback режим: берем последние 30 постов
                messages_to_process = all_messages[:30]
                actual_period_text = "последние 30 постов"
                logger.info(f"Fallback режим: анализируем {len(messages_to_process)} постов")
            else:
                # Нормальный режим: фильтруем по временному диапазону
                for msg in all_messages:
                    if not msg.date:
                        continue
                        
                    msg_time = msg.date.replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                    if start_time <= msg_time <= end_time:
                        messages_to_process.append(msg)
                
                logger.info(f"Нормальный режим: найдено {len(messages_to_process)} постов за период")
            
            # Если в нормальном режиме нет постов, но канал активный (последний пост < 30 дней)
            # то все равно показываем, что постов нет за период
            if not used_fallback and not messages_to_process:
                return {
                    'channel_info': channel_info,
                    'analysis_period': {
                        'hours_back': hours_back,
                        'start_time': start_time.strftime('%d.%m.%Y %H:%M'),
                        'end_time': end_time.strftime('%d.%m.%Y %H:%M'),
                        'actual_period': actual_period_text,
                        'used_fallback': False
                    },
                    'total_posts': 0,
                    'message': 'Нет постов за указанный период',
                    'last_message_date': last_message_date.strftime('%Y-%m-%d %H:%M') if last_message_date else 'Неизвестно'
                }
            
            # Группируем сообщения по grouped_id
            grouped_messages = defaultdict(list)
            single_messages = []
            
            for msg in messages_to_process:
                if not msg.date:
                    continue
                    
                if hasattr(msg, 'grouped_id') and msg.grouped_id:
                    grouped_messages[msg.grouped_id].append(msg)
                else:
                    single_messages.append(msg)
            
            logger.info(f"Обработано постов: {len(messages_to_process)} (групп: {len(grouped_messages)}, одиночных: {len(single_messages)})")
            
            if not messages_to_process:
                return {
                    'channel_info': channel_info,
                    'analysis_period': {
                        'hours_back': hours_back,
                        'start_time': start_time.strftime('%d.%m.%Y %H:%M'),
                        'end_time': end_time.strftime('%d.%m.%Y %H:%M'),
                        'actual_period': actual_period_text,
                        'used_fallback': used_fallback
                    },
                    'total_posts': 0,
                    'message': 'Нет постов для анализа',
                    'last_message_date': last_message_date.strftime('%Y-%m-%d %H:%M') if last_message_date else 'Неизвестно'
                }
            
            # Обрабатываем группы сообщений
            processed_posts = []
            
            # Обрабатываем группы
            for group_id, messages in grouped_messages.items():
                group_post = self._process_message_group(messages)
                if group_post:
                    processed_posts.append(group_post)
            
            # Обрабатываем одиночные сообщения
            for msg in single_messages:
                moscow_time = msg.date.replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                text_preview = (msg.text[:100] + '...') if msg.text and len(msg.text) > 100 else msg.text or 'Медиа контент'
                
                processed_posts.append({
                    'id': msg.id,
                    'date': msg.date,
                    'views': self._get_views(msg),
                    'reactions': self._get_reactions(msg),
                    'forwards': self._get_forwards(msg),
                    'comments': self._get_comments(msg),
                    'text_preview': text_preview,
                    'content_type': self._categorize_single_content(msg),
                    'is_group': False,
                    'group_size': 1
                })
            
            logger.info(f"Обработано постов: {len(processed_posts)} (групп: {len(grouped_messages)}, одиночных: {len(single_messages)})")
            
            # Анализируем данные с использованием безопасных методов
            total_posts = len(processed_posts)
            total_views = sum(post['views'] for post in processed_posts)
            total_reactions = sum(post['reactions'] for post in processed_posts)
            total_comments = sum(post['comments'] for post in processed_posts)
            total_forwards = sum(post['forwards'] for post in processed_posts)
            
            # Анализ по типам контента
            content_stats = {}
            for post in processed_posts:
                content_type = post['content_type']
                if content_type not in content_stats:
                    content_stats[content_type] = {
                        'count': 0, 
                        'total_views': 0, 
                        'total_reactions': 0,
                        'total_comments': 0,
                        'total_forwards': 0
                    }
                content_stats[content_type]['count'] += 1
                content_stats[content_type]['total_views'] += post['views']
                content_stats[content_type]['total_reactions'] += post['reactions']
                content_stats[content_type]['total_comments'] += post['comments']
                content_stats[content_type]['total_forwards'] += post['forwards']
            
            # ТОП постов
            top_posts = sorted(processed_posts, key=lambda x: x['views'], reverse=True)[:5]
            top_posts_data = []
            for post in top_posts:
                moscow_time = post['date'].replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                
                post_type = post['content_type']
                if post['is_group']:
                    post_type = f"{post_type} (альбом из {post['group_size']})"
                
                top_posts_data.append({
                    'id': post['id'],
                    'date': moscow_time.strftime('%d.%m.%Y %H:%M'),
                    'views': post['views'],
                    'reactions': post['reactions'],
                    'forwards': post['forwards'],
                    'text_preview': post['text_preview'],
                    'content_type': post_type,
                    'is_group': post['is_group'],
                    'group_size': post['group_size']
                })
            
            # Анализ времени
            time_analysis = self.get_time_analysis(processed_posts)
            
            # Расчет engagement rate
            subscribers = channel_info.get('subscribers', 0)
            avg_engagement = self.calculate_engagement_rate(
                total_views, total_reactions, total_comments, 
                total_forwards, subscribers
            )
            
            # Генерируем рекомендации
            recommendations = self.generate_recommendations(
                content_stats, time_analysis, avg_engagement, total_posts, hours_back
            )
            
            # Формируем итоговый отчет
            report = {
                'channel_info': channel_info,
                'analysis_period': {
                    'hours_back': hours_back,
                    'start_time': start_time.strftime('%d.%m.%Y %H:%M') if not used_fallback else None,
                    'end_time': end_time.strftime('%d.%m.%Y %H:%M') if not used_fallback else None,
                    'actual_period': actual_period_text,
                    'used_fallback': used_fallback,
                    'fallback_reason': fallback_reason
                },
                'summary': {
                    'total_posts': total_posts,
                    'total_views': total_views,
                    'avg_views_per_post': round(total_views / total_posts, 1) if total_posts > 0 else 0,
                    'total_reactions': total_reactions,
                    'total_comments': total_comments,
                    'total_forwards': total_forwards,
                    'engagement_rate': avg_engagement
                },
                'content_analysis': content_stats,
                'time_analysis': time_analysis,
                'top_posts': top_posts_data,
                'recommendations': recommendations,
                'generated_at': datetime.now(self.moscow_tz).strftime('%d.%m.%Y %H:%M:%S'),
                'group_processing_info': {
                    'groups_processed': len(grouped_messages),
                    'single_messages': len(single_messages)
                },
                'last_message_date': last_message_date.strftime('%Y-%m-%d %H:%M') if last_message_date else 'Неизвестно'
            }
            
            return report
            
        except FloodWaitError as e:
            logger.error(f"Flood wait error: {e.seconds} seconds")
            return {'error': f'Превышен лимит запросов. Попробуйте через {e.seconds} секунд'}
        except Exception as e:
            logger.error(f"Ошибка анализа канала: {str(e)}", exc_info=True)
            return {'error': f'Ошибка анализа: {str(e)}'}
    
    def _categorize_single_content(self, message):
        """Категоризация типа контента для одиночных сообщений"""
        if message.media:
            if isinstance(message.media, MessageMediaPhoto):
                return 'photo'
            elif isinstance(message.media, MessageMediaDocument):
                if message.media.document:
                    mime_type = message.media.document.mime_type
                    if mime_type.startswith('video/'):
                        return 'video'
                    elif mime_type.startswith('audio/'):
                        return 'audio'
                    else:
                        return 'document'
            else:
                return 'media'
        elif message.text:
            return 'text'
        else:
            return 'other'
    
    def get_time_analysis(self, posts):
        """Анализ времени публикаций на основе обработанных постов"""
        hour_stats = {}
        for post in posts:
            if post['date']:
                moscow_time = post['date'].replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                hour = moscow_time.hour
                if hour not in hour_stats:
                    hour_stats[hour] = {'count': 0, 'total_views': 0}
                hour_stats[hour]['count'] += 1
                hour_stats[hour]['total_views'] += post['views']
        
        # Находим наиболее активные часы
        if hour_stats:
            best_hours = sorted(hour_stats.items(), 
                              key=lambda x: x[1]['total_views'] / x[1]['count'] if x[1]['count'] > 0 else 0, 
                              reverse=True)[:3]
        else:
            best_hours = []
        
        return {
            'hourly_stats': hour_stats,
            'best_hours': [{'hour': h[0], 'avg_views': h[1]['total_views'] / h[1]['count']} 
                          for h in best_hours] if best_hours else []
        }
    
    def calculate_engagement_rate(self, views, reactions, comments, forwards, subscribers):
        # Защита от нулевых значений и аномалий
        if views == 0 or subscribers == 0:
            return {
                'er_views': 0,
                'er_subscribers': 0,
                'er_quality': 'low'  # Добавляем показатель качества данных
            }
        
        total_interactions = reactions + comments + forwards
        
        # Проверка на аномально высокие значения
        subs_er = (total_interactions / subscribers) * 100
        if subs_er > 100:  # Нереалистично высокий ER
            subs_er = 0
            er_quality = 'questionable'
        else:
            er_quality = 'normal'
        
        return {
            'er_views': round((total_interactions / views) * 100, 2),
            'er_subscribers': round(subs_er, 2),
            'er_quality': er_quality
        }

    def generate_recommendations(self, content_stats, time_analysis, engagement, total_posts, hours_back):
        """Генерация рекомендаций"""
        recommendations = []
        
        # Анализ типов контента
        if content_stats:
            # Находим контент с максимальным средним просмотром
            best_content = max(
                [(ctype, stats) for ctype, stats in content_stats.items() if stats['count'] > 0],
                key=lambda x: x[1]['total_views'] / x[1]['count'],
                default=None
            )
            
            if best_content:
                avg_views = best_content[1]['total_views'] / best_content[1]['count']
                recommendations.append(
                    f"🎯 Наиболее эффективный тип контента: {best_content[0]} "
                    f"(среднее {avg_views:.0f} просмотров)"
                )
        
        # Анализ времени публикации
        if time_analysis.get('best_hours'):
            best_hour = time_analysis['best_hours'][0]['hour']
            recommendations.append(
                f"⏰ Оптимальное время для публикаций: {best_hour}:00-{best_hour+1}:00 МСК"
            )
        
        # Анализ активности
        posts_per_day = total_posts / (hours_back / 24) if hours_back > 0 else 0
        if posts_per_day < 1:
            recommendations.append("📈 Рекомендуется увеличить частоту публикаций (минимум 1 пост в день)")
        elif posts_per_day > 5:
            recommendations.append("⚠️ Возможно, стоит снизить частоту публикаций для лучшего engagement")
        
        # Анализ engagement rate
        if engagement['er_views'] < 1:
            recommendations.append("💡 Низкий engagement rate. Попробуйте более интерактивный контент")
        elif engagement['er_views'] > 5:
            recommendations.append("🔥 Отличный engagement rate! Продолжайте в том же духе")
        
        return recommendations

# Создаем экземпляр аналитики
analytics = TelegramAnalytics()

# Flask маршруты
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'telegram-analytics',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/analyze', methods=['POST'])
def perform_analysis():
    try:
        # Проверяем, есть ли данные
        if not request.data:
            return jsonify({'error': 'No data provided'}), 400
            
        # Логируем сырые данные для отладки
        raw_data = request.data.decode('utf-8')
        logger.info(f"Received raw data: {raw_data}")
        
        try:
            data = request.get_json()
        except Exception as e:
            logger.error(f"JSON parsing error: {str(e)}")
            return jsonify({'error': 'Invalid JSON format'}), 400
        
        channel_identifier = data.get('channel_username') or data.get('channel_id')
        hours_back = data.get('hours_back', 24)
        
        if not channel_identifier:
            return jsonify({'error': 'Не указан username или ID канала'}), 400
        
        # Добавляем проверку типа hours_back
        try:
            hours_back = int(hours_back)
        except (ValueError, TypeError):
            hours_back = 24
        
        # Получаем глобальный event loop
        loop = current_app.config['GLOBAL_EVENT_LOOP']
        
        # Запускаем анализ
        result = loop.run_until_complete(analytics.analyze_channel(channel_identifier, hours_back))
        
        # # Если нет ошибки, добавляем ИИ анализ
        # if 'error' not in result:
            # logger.info("Запуск ИИ анализа...")
            # ai_report = loop.run_until_complete(analytics.generate_ai_analysis(result))
            # logger.info(f"ИИ анализ завершен, длина: {len(ai_report)} символов")
            # result['ai_report'] = ai_report
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Ошибка при выполнении анализа: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/ai_analyze', methods=['POST'])
def ai_analyze():
    """Эндпоинт для ИИ анализа с улучшенным логированием"""
    try:
        data = request.get_json()
        report_data = data.get('report')
        
        if not report_data:
            return jsonify({'error': 'No report data provided'}), 400
        
        # Получаем период анализа из отчета
        hours_back = report_data['analysis_period']['hours_back']
        
        # Детальное логирование полученных данных
        logger.info(f"Получен запрос на ИИ анализ для канала: {report_data['channel_info']['title']}")
        logger.info(f"Период анализа: {hours_back} часов")
        logger.debug(f"ID канала: {report_data['channel_info']['id']}")
        
        channel_id = report_data['channel_info']['id']
        
        # Проверяем кэш в Supabase с учетом периода анализа
        try:
            logger.info(f"Проверка кэша в Supabase для channel_id: {channel_id}, период: {hours_back} часов")
            response = requests.get(
                f"{SUPABASE_URL}/rest/v1/ai_reports?channel_id=eq.{channel_id}&hours_back=eq.{hours_back}&order=created_at.desc&limit=1",
                headers=SUPABASE_HEADERS,
                timeout=5
            )
            
            if response.status_code == 200:
                cached_data = response.json()
                # Если есть свежий (менее 1 часа) кэш - возвращаем его
                if cached_data and len(cached_data) > 0:
                    created_at_str = cached_data[0]['created_at']
                    try:
                        # Преобразуем строку в datetime с учетом временной зоны
                        created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                        now_utc = datetime.now(pytz.UTC)
                        
                        # Проверяем разницу во времени
                        if (now_utc - created_at).total_seconds() < 3600:
                            logger.info(f"Найден свежий кэш в Supabase (created_at: {created_at})")
                            return jsonify({
                                'ai_report': cached_data[0]['report_data'],
                                'cached': True
                            })
                        else:
                            logger.info(f"Кэш устарел (разница: {(now_utc - created_at).total_seconds()/60:.1f} минут)")
                    except Exception as e:
                        logger.error(f"Ошибка парсинга даты: {str(e)}")
            else:
                logger.warning(f"Supabase cache check failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            logger.warning(f"Не удалось проверить кэш Supabase: {str(e)}")
        
        # Получаем глобальный event loop
        loop = current_app.config['GLOBAL_EVENT_LOOP']
        
        # Запускаем ИИ анализ через event loop
        logger.info("Запуск ИИ анализа...")
        ai_report = loop.run_until_complete(analytics.generate_ai_analysis(report_data))
        logger.info("ИИ анализ завершен")
        
        # Сохраняем в Supabase с указанием периода анализа
        try:
            logger.info("Сохранение результата в Supabase...")
            response = requests.post(
                f"{SUPABASE_URL}/rest/v1/ai_reports",
                headers=SUPABASE_HEADERS,
                json={
                    'channel_id': channel_id,
                    'report_data': ai_report,
                    'hours_back': hours_back  # Добавляем период анализа
                },
                timeout=10
            )
            
            if response.status_code in (200, 201):
                logger.info("Результат успешно сохранен в Supabase")
            else:
                logger.warning(f"Supabase save error: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить в БД: {str(e)}")
        
        return jsonify({'ai_report': ai_report})
    
    except Exception as e:
        logger.error(f"Ошибка ИИ анализа: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/channel_subscribers', methods=['POST'])
def get_channel_subscribers():
    """Получение количества подписчиков"""
    try:
        data = request.get_json()
        channel_identifier = data.get('channel_username') or data.get('channel_id')
        
        if not channel_identifier:
            return jsonify({'error': 'Не указан username или ID канала'}), 400
        
        # Получаем глобальный event loop
        loop = current_app.config['GLOBAL_EVENT_LOOP']
        
        # Получаем информацию о канале
        result = loop.run_until_complete(analytics.get_channel_info(channel_identifier))
        
        if result and 'error' not in result:
            return jsonify({
                'channel': result['title'],
                'username': result.get('username', ''),
                'subscribers': result.get('subscribers', 0),
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({'error': 'Канал не найден'}), 404
            
    except Exception as e:
        logger.error(f"Ошибка в channel_subscribers: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/find_channel', methods=['POST'])
async def search_channels(query):
    """Поиск каналов по запросу"""
    if not analytics.client or not analytics.client.is_connected():
        await analytics.init_client()
    
    results = []
    
    try:
        # Попробуем найти канал напрямую по username
        try:
            entity = await analytics.client.get_entity(query)
            if entity and (isinstance(entity, Channel) or isinstance(entity, ChannelForbidden)):
                results.append({
                    'id': entity.id,
                    'title': entity.title,
                    'username': getattr(entity, 'username', None),
                    'is_channel': True
                })
                return results
        except Exception:
            pass
        
        # Если прямой поиск не дал результатов, ищем в диалогах
        async for dialog in analytics.client.iter_dialogs():
            if dialog.is_channel:
                # Проверяем несколько вариантов совпадения
                title_match = query.lower() in dialog.name.lower()
                username_match = False
                
                # Проверяем username канала
                if hasattr(dialog.entity, 'username') and dialog.entity.username:
                    username_match = query.lower() == dialog.entity.username.lower()
                
                if title_match or username_match:
                    results.append({
                        'id': dialog.entity.id,
                        'title': dialog.name,
                        'username': getattr(dialog.entity, 'username', None),
                        'is_channel': True
                    })
    except Exception as e:
        logger.error(f"Ошибка поиска канала: {str(e)}", exc_info=True)
    
    return results

@app.route('/channel_history', methods=['POST'])
def get_channel_history():
    """Получение истории постов из канала (последние 20-30 текстовых постов)"""
    try:
        data = request.get_json()
        channel_identifier = data.get('channel_username') or data.get('channel_id')
        limit = min(int(data.get('limit', 30)), 50)  # Максимум 50 постов
        
        if not channel_identifier:
            return jsonify({'error': 'Не указан username или ID канала'}), 400

        # Получаем глобальный event loop
        loop = current_app.config['GLOBAL_EVENT_LOOP']
        
        # Запускаем получение истории
        result = loop.run_until_complete(analytics.get_channel_history(channel_identifier, limit))
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Ошибка при получении истории канала: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/download_pdf', methods=['GET'])
def download_pdf():
    """Прямое скачивание PDF файла для мобильных устройств"""
    try:
        # Получаем параметры из запроса
        cache_key = request.args.get('key')
        
        if not cache_key:
            logger.error("Не указан ключ доступа для скачивания PDF")
            return jsonify({'error': 'Не указан ключ доступа'}), 400
        
        logger.info(f"Запрос на скачивание PDF с ключом: {cache_key}")
        logger.info(f"Доступные ключи в кэше: {list(pdf_cache.keys())}")
        
        # Проверяем наличие PDF в кэше
        if cache_key not in pdf_cache:
            logger.error(f"PDF с ключом {cache_key} не найден в кэше")
            return jsonify({'error': 'PDF не найден или срок действия ссылки истек'}), 404
        
        cached_data = pdf_cache[cache_key]
        
        # Проверяем не истекло ли время кэша
        if time.time() - cached_data['timestamp'] > CACHE_EXPIRY:
            # Удаляем из кэша
            del pdf_cache[cache_key]
            logger.error(f"Срок действия ключа {cache_key} истек")
            return jsonify({'error': 'Срок действия ссылки истек'}), 404
        
        # Возвращаем PDF как файл для скачивания
        response = make_response(cached_data['pdf_data'])
        response.headers['Content-Type'] = 'application/pdf'
        
        # Безопасное имя файла
        safe_filename = get_safe_filename(cached_data['filename'])
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_filename}"'
        
        logger.info(f"PDF успешно отправлен: {cached_data['filename']}")
        
        return response
        
    except Exception as e:
        logger.error(f"Ошибка скачивания PDF: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf():
    """Генерация PDF отчета с поддержкой кириллицы"""
    try:
        # Добавляем импорт Paragraph
        from reportlab.platypus import Paragraph
        
        # Используем шрифты с поддержкой кириллицы
        if CYRILLIC_FONT_AVAILABLE:
            base_font = 'DejaVuSans'
            bold_font = 'DejaVuSans-Bold'
        else:
            # Fallback на стандартные шрифты
            base_font = 'Helvetica'
            bold_font = 'Helvetica-Bold'

        data = request.get_json()
        report_data = data.get('report')
        ai_report = data.get('ai_report', '')
        
        if not report_data:
            return jsonify({'error': 'No report data provided'}), 400

        # Создаем буфер для PDF
        buffer = BytesIO()
        
        # Инициализация документа с UTF-8 кодировкой
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=30,
            leftMargin=30,
            topMargin=30,
            bottomMargin=30,
            encoding='utf-8'
        )
        
        # Стили для текста
        styles = getSampleStyleSheet()
        
        # Переопределяем стандартные стили для поддержки кириллицы
        styles['Normal'].fontName = base_font
        styles['BodyText'].fontName = base_font
        styles['Italic'].fontName = base_font
        styles['Heading1'].fontName = bold_font
        styles['Heading2'].fontName = bold_font
        styles['Heading3'].fontName = bold_font
        
        # Основные стили
        styles.add(ParagraphStyle(
            name='NormalRU',
            fontName=base_font,
            fontSize=10,
            leading=12,
            spaceAfter=6
        ))
        
        styles.add(ParagraphStyle(
            name='HeaderRU',
            fontName=bold_font,
            fontSize=14,
            textColor=colors.HexColor('#3B82F6'),
            spaceAfter=12
        ))
        
        styles.add(ParagraphStyle(
            name='SubheaderRU', 
            fontName=bold_font,
            fontSize=12,
            textColor=colors.HexColor('#2563EB'),
            spaceAfter=8
        ))

        styles.add(ParagraphStyle(
            name='SmallRU',
            fontName=base_font,
            fontSize=8,
            textColor=colors.HexColor('#666666'),
            spaceAfter=4,
            leading=10
        ))
        
        # Добавляем стиль для жирного текста
        styles.add(ParagraphStyle(
            name='BoldRU',
            fontName=bold_font,
            fontSize=11,
            leading=13,
            spaceAfter=8,
            spaceBefore=12
        ))

        # Определяем стили для мобильных устройств
        user_agent = request.headers.get('User-Agent', '')
        is_mobile = any(device in user_agent.lower() for device in ['mobile', 'android', 'iphone', 'ipad'])

        if is_mobile:
            logger.info("Mobile device detected - adjusting font sizes")
            styles['NormalRU'].fontSize = 9
            styles['HeaderRU'].fontSize = 12
            styles['SubheaderRU'].fontSize = 10
            styles['SmallRU'].fontSize = 7

        elements = []
        
        # Заголовок - сохраняем смайлы как есть
        title = report_data['channel_info']['title']
        elements.append(Paragraph(
            f"Аналитический отчет: {title}",
            styles['HeaderRU']
        ))
        
        # Период анализа
        elements.append(Paragraph(
            f"Период анализа: {report_data['analysis_period']['hours_back']} часов",
            styles['NormalRU']
        ))
        elements.append(Spacer(1, 20))
        
        # Основные метрики
        metrics = [
            ['Метрика', 'Значение'],
            ['Подписчиков', str(report_data['channel_info']['subscribers'])],
            ['Всего постов', str(report_data['summary']['total_posts'])],
            ['Всего просмотров', str(report_data['summary']['total_views'])],
            ['Средний охват', str(round(report_data['summary']['avg_views_per_post'], 1))],
            ['ER (просмотры)', f"{report_data['summary']['engagement_rate']['er_views']}%"],
            ['ER (подписчики)', f"{report_data['summary']['engagement_rate']['er_subscribers']}%"]
        ]
        
        metrics_table = Table(metrics, colWidths=[200, 100])
        metrics_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3F4F6')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1F2937')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), bold_font),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB')),
            ('FONTNAME', (0, 1), (-1, -1), base_font),
            ('WORDWRAP', (0, 0), (-1, -1), True)
        ]))
        
        elements.append(metrics_table)
        elements.append(Spacer(1, 30))
        
        # Рекомендации - сохраняем смайлы как есть
        elements.append(Paragraph("Рекомендации", styles['HeaderRU']))
        for rec in report_data.get('recommendations', []):
            # Только заменяем названия типов контента, смайлы оставляем
            rec = rec.replace('mixed_media_with_text', 'текст + медиа')
            rec = rec.replace('text', 'текст')
            rec = rec.replace('photo', 'фото')
            rec = rec.replace('video', 'видео')
            rec = rec.replace('media', 'медиа')
            
            elements.append(Paragraph(f"• {rec}", styles['NormalRU']))
        elements.append(Spacer(1, 20))
        
        # Анализ ИИ - сохраняем смайлы как есть
        # Замените блок обработки AI-отчета на этот:

        if ai_report:
            elements.append(Paragraph("ИИ Анализ", styles['HeaderRU']))
            elements.append(Spacer(1, 12))

            # Если есть пометка о кэше — добавляем её
            if "кэша" in ai_report:
                cache_line = ai_report.split('\n')[0]  # Берём первую строку (кэш)
                elements.append(Paragraph(cache_line, styles['SmallRU']))
                elements.append(Spacer(1, 12))
                # Убираем строку кэша из основного отчёта
                ai_report = '\n'.join(ai_report.split('\n')[1:])

            # Обрабатываем AI отчет - заменяем англоязычные термины
            ai_report = ai_report.replace('mixed_media_with_text', 'текст + медиа')
            ai_report = ai_report.replace('text', 'текст')
            ai_report = ai_report.replace('photo', 'фото')
            ai_report = ai_report.replace('video', 'видео')
            ai_report = ai_report.replace('media', 'медиа')

            # Разделяем отчет на секции по двойным переносам
            sections = ai_report.split('\n\n')

            for section in sections:
                section = section.strip()
                if not section:
                    continue

                # Удаляем все ** из секции
                section = section.replace('**', '')

                # Разделяем секцию на строки
                lines = section.split('\n')
                
                # Проверяем, является ли первая строка заголовком (начинается с цифры)
                if lines and re.match(r'^\d+\.', lines[0].strip()):
                    # Это заголовок - делаем жирным
                    elements.append(Paragraph(lines[0].strip(), styles['BoldRU']))
                    elements.append(Spacer(1, 8))
                    
                    # Обрабатываем остальные строки как обычный текст
                    for line in lines[1:]:
                        line = line.strip()
                        if not line:
                            continue
                        
                        if line.startswith('-'):
                            # Элемент списка
                            elements.append(Paragraph(f"• {line[1:].strip()}", styles['NormalRU']))
                        else:
                            # Обычная строка
                            elements.append(Paragraph(line, styles['NormalRU']))
                else:
                    # Вся секция - обычный текст
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        
                        if line.startswith('-'):
                            # Элемент списка
                            elements.append(Paragraph(f"• {line[1:].strip()}", styles['NormalRU']))
                        else:
                            # Обычная строка
                            elements.append(Paragraph(line, styles['NormalRU']))
                
                elements.append(Spacer(1, 8))
        
        # Топ постов - сохраняем смайлы как есть
        if report_data.get('top_posts'):
            elements.append(Paragraph("Топ постов", styles['HeaderRU']))
            elements.append(Spacer(1, 10))
            
            # Упрощенный формат списка вместо таблицы
            for i, post in enumerate(report_data['top_posts'][:3], 1):
                content_type = post.get('content_type', '')
                content_type = content_type.replace('mixed_media_with_text', 'текст + медиа')
                content_type = content_type.replace('text', 'текст')
                content_type = content_type.replace('photo', 'фото')
                content_type = content_type.replace('video', 'видео')
                
                preview = post.get('text_preview', '')
                # Сохраняем смайлы в превью
                if len(preview) > 60:
                    preview = preview[:57] + '...'
                
                # Используем простой список вместо таблицы
                post_info = f"{i}. {post.get('date', '')} - {post.get('views', 0)} просмотров"
                elements.append(Paragraph(post_info, styles['NormalRU']))
                elements.append(Paragraph(f"   Тип: {content_type}", styles['SmallRU']))
                if preview:
                    elements.append(Paragraph(f"   {preview}", styles['SmallRU']))
                elements.append(Spacer(1, 10))
        
        # Создаем PDF
        doc.build(elements)
        
        # Подготовка ответа
        buffer.seek(0)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        filename = get_safe_filename(report_data['channel_info'])
        
        # Создаем ключ для кэша
        cache_key = hashlib.md5(f"{report_data['channel_info']['title']}_{time.time()}".encode()).hexdigest()
        
        pdf_cache[cache_key] = {
            'pdf_data': pdf_data,
            'filename': filename,
            'timestamp': time.time()
        }
        
        logger.info(f"PDF сохранен в кэше с ключом: {cache_key}")
        cleanup_pdf_cache()
        
        is_direct_download = request.args.get('direct') == 'true'
        
        if is_direct_download:
            response = make_response(pdf_data)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'attachment; filename="{get_safe_filename(filename)}"'
            return response
        else:
            return jsonify({
                'pdf_base64': base64.b64encode(pdf_data).decode('utf-8'),
                'filename': filename,
                'cache_key': cache_key
            })
    
    except Exception as e:
        logger.error(f"Ошибка генерации PDF: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500
 
def cleanup_pdf_cache():
    """Очистка устаревших PDF из кэша (только для подстраховки)"""
    current_time = time.time()
    keys_to_delete = []
    
    for key, data in pdf_cache.items():
        if current_time - data['timestamp'] > CACHE_EXPIRY:
            keys_to_delete.append(key)
    
    for key in keys_to_delete:
        del pdf_cache[key]
    
    if keys_to_delete:
        logger.info(f"Очищено {len(keys_to_delete)} устаревших PDF из кэша")
 
# Отдача фронтенда
@app.route('/')
def home():
    # Создаем базовый HTML файл если его нет
    if not os.path.exists('static/index.html'):
        create_basic_html()
    return send_from_directory('static', 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

if __name__ == '__main__':
    # Создаем event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Создаем папки
        os.makedirs('static/fonts', exist_ok=True)
        
        # Сохраняем loop в конфиг приложения
        app.config['GLOBAL_EVENT_LOOP'] = loop
        
        # Проверка подключения к Supabase
        logger.info("Проверка подключения к Supabase...")
        try:
            response = requests.get(
                f"{SUPABASE_URL}/rest/v1/ai_reports?select=*&limit=1",
                headers=SUPABASE_HEADERS,
                timeout=10
            )
            if response.status_code == 401:
                logger.error("ОШИБКА: Неверные учетные данные Supabase!")
            elif response.status_code == 200:
                logger.info("Подключение к Supabase успешно")
            else:
                logger.error(f"Ошибка подключения к Supabase: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Ошибка подключения к Supabase: {str(e)}")
            
        # Инициализация клиента Telegram
        logger.info("Инициализация Telegram клиента...")
        try:
            init_result = loop.run_until_complete(analytics.init_client())
            if not init_result:
                logger.warning("Не удалось инициализировать Telegram клиент. Будет инициализирован при первом запросе.")
        except Exception as e:
            logger.error(f"Ошибка инициализации Telegram клиента: {str(e)}", exc_info=True)
        
        # Получаем порт из переменных окружения
        port = int(os.getenv('PORT', 5050))
        
        # Запуск Flask
        logger.info(f"Запуск Flask приложения на порту {port}...")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
    except Exception as e:
        logger.error(f"Ошибка запуска приложения: {str(e)}", exc_info=True)
    finally:
        logger.info("Завершение работы приложения...")
        # Корректно закрываем event loop
        if loop:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()