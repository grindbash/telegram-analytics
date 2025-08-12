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
from flask import Flask, request, jsonify
from telethon.sync import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.errors import FloodWaitError, SessionPasswordNeededError, ChannelPrivateError
from telethon.tl.types import PeerChannel
from telethon.tl.functions.channels import GetFullChannelRequest
from dotenv import load_dotenv
from flask import Flask, send_from_directory, jsonify
from supabase import create_client, Client
import requests
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from io import BytesIO
import base64

# Устанавливаем UTF-8 как стандартную кодировку
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Загружаем переменные окружения
load_dotenv()

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
loop = asyncio.new_event_loop()

def start_event_loop():
    """Запуск глобального цикла событий в отдельном потоке"""
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Запускаем цикл событий в фоновом потоке
threading.Thread(target=start_event_loop, daemon=True).start()

# Инициализация Flask
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # Для корректного отображения русского языка в JSON

# Конфигурация
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
#PHONE = os.getenv('PHONE')
#PWD = os.getenv('TG_PWD')
SESSION_PATH = os.getenv('analytics_session')  # Файл сессии в текущей директории

# Конфигурация Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Конфигурация OpenRouter
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = "deepseek/deepseek-chat-v3-0324:free"  # или "qwen/qwen-72b-chat" для Qwen

class TelegramAnalytics:
    def __init__(self):
        self.client = None
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        self.loop = loop
    
    async def init_client(self):
        """Инициализация Telegram клиента"""
        try:
            # Проверяем существует ли файл сессии
            session_exists = os.path.exists(SESSION_PATH)
            
            self.client = TelegramClient(
                SESSION_PATH, 
                API_ID, 
                API_HASH,
                loop=self.loop
            )
            
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
    
    async def generate_ai_analysis(self, report_data):
        """Генерация ИИ анализа через OpenRouter"""
        try:
            prompt = f"""
            Ты эксперт по анализу Telegram каналов. Проанализируй данные и дай рекомендации.
            
            Контекст:
            - Канал: {report_data['channel_info']['title']}
            - Подписчиков: {report_data['channel_info']['subscribers']}
            - Период анализа: {report_data['analysis_period']['hours_back']} часов
            
            Данные для анализа:
            {json.dumps(report_data['summary'], indent=2)}
            
            Требования:
            1. Выяви ключевые тенденции
            2. Дай рекомендации по контенту
            3. Предложи оптимальное время публикаций
            4. Оцени вовлеченность аудитории
            5. Спрогнозируй рост на следующий период
            """
            
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "Ты профессиональный аналитик Telegram каналов"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000
            }
            
            response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            return result['choices'][0]['message']['content']
            
        except Exception as e:
            logger.error(f"Ошибка ИИ анализа: {str(e)}", exc_info=True)
            return f"Ошибка при генерации ИИ анализа: {str(e)}"
 
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
        """Основной метод анализа канала с учётом группировки сообщений"""
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
            
            # Временной диапазон
            end_time = datetime.now(self.moscow_tz)
            start_time = end_time - timedelta(hours=hours_back)
            
            logger.info(f"Анализ канала: {channel_info['title']}")
            logger.info(f"Текущее время сервера: {datetime.now(self.moscow_tz)}")
            logger.info(f"Диапазон анализа: {start_time} - {end_time}")
            
            # Получаем сообщения
            all_messages = []
            last_message_date = None
            try:
                # Получаем все доступные сообщения
                all_messages = await self.client.get_messages(
                    channel_identifier, 
                    limit=1000  # Лимит сообщений для анализа
                )
                
                logger.info(f"Получено сообщений: {len(all_messages)}")
                
            except ChannelPrivateError:
                return {
                    'error': 'Приватный канал',
                    'message': 'У вас нет доступа к этому каналу. Убедитесь что вы подписаны.'
                }
            except Exception as e:
                logger.error(f"Ошибка получения сообщений: {str(e)}", exc_info=True)
                return {'error': f'Ошибка получения сообщений: {str(e)}'}
            
            # Группируем сообщения по grouped_id
            grouped_messages = defaultdict(list)
            single_messages = []
            
            for msg in all_messages:
                if not msg.date:
                    continue
                    
                msg_time = msg.date.replace(tzinfo=pytz.UTC).astimezone(self.moscow_tz)
                if not last_message_date:
                    last_message_date = msg_time
                
                # Фильтруем по временному диапазону
                if start_time <= msg_time <= end_time:
                    if hasattr(msg, 'grouped_id') and msg.grouped_id:
                        grouped_messages[msg.grouped_id].append(msg)
                    else:
                        single_messages.append(msg)
            
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
            
            if not processed_posts:
                return {
                    'channel_info': channel_info,
                    'period': f'{hours_back} часов',
                    'total_posts': 0,
                    'message': 'Нет постов за указанный период',
                    'last_message_date': last_message_date.strftime('%Y-%m-%d %H:%M') if last_message_date else 'Неизвестно'
                }
            
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
                    'start_time': start_time.strftime('%d.%m.%Y %H:%M'),
                    'end_time': end_time.strftime('%d.%m.%Y %H:%M')
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
                }
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

# Flask маршруты (обновлённые)
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'telegram-analytics',
        'timestamp': datetime.now().isoformat()
    })

# Новое название endpoint и функции
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
        
        # Запускаем анализ
        future = asyncio.run_coroutine_threadsafe(
            analytics.analyze_channel(channel_identifier, hours_back),
            loop
        )
        result = future.result(timeout=300)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Ошибка при выполнении анализа: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# Роут для AI анализа + запись в БД
@app.route('/ai_analyze', methods=['POST'])
def ai_analyze():
    """Эндпоинт для ИИ анализа"""
    try:
        data = request.get_json()
        report_data = data.get('report')
        
        if not report_data:
            return jsonify({'error': 'No report data provided'}), 400
        
        channel_id = report_data['channel_info']['id']
        
        # Проверяем кэш в Supabase
        cached_response = (
            supabase.table('ai_reports')
            .select('*')
            .eq('channel_id', channel_id)
            .order('created_at', desc=True)
            .limit(1)
            .execute()
        )
        
        # Если есть свежий (менее 1 часа) кэш - возвращаем его
        if (cached_response.data and 
            (datetime.now() - datetime.fromisoformat(cached_response.data[0]['created_at'])).total_seconds() < 3600):
            return jsonify({
                'ai_report': cached_response.data[0]['report_data'],
                'cached': True
            })
        
        # Запускаем ИИ анализ
        future = asyncio.run_coroutine_threadsafe(
            analytics.generate_ai_analysis(report_data),
            loop
        )
        ai_report = future.result(timeout=300)
        
        # Сохраняем в Supabase
        supabase.table('ai_reports').insert({
            'channel_id': channel_id,
            'report_data': ai_report
        }).execute()
        
        # Удаляем старые записи (оставляем 5)
        supabase.rpc('keep_recent_reports', {
            'p_channel_id': channel_id,
            'p_keep_count': 5
        }).execute()
        
        return jsonify({'ai_report': ai_report})
    
    except Exception as e:
        logger.error(f"Ошибка ИИ анализа: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# Роут для AI анализа - настройки анализа
@app.route('/ai_settings', methods=['POST'])
def save_ai_settings():
    """Сохранение настроек ИИ анализа"""
    try:
        data = request.get_json()
        channel_id = data.get('channel_id')
        
        if not channel_id:
            return jsonify({'error': 'Channel ID is required'}), 400
        
        # Upsert настроек
        supabase.table('ai_settings').upsert({
            'channel_id': channel_id,
            'focus_areas': data.get('focus_areas'),
            'niche': data.get('niche')
        }).execute()
        
        return jsonify({'status': 'success'})
    
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Роут для генерации PDF
@app.route('/generate_pdf', methods=['POST'])
def generate_pdf():
    """Генерация PDF отчета"""
    try:
        data = request.get_json()
        report_data = data.get('report')
        ai_report = data.get('ai_report', '')
        
        if not report_data:
            return jsonify({'error': 'No report data provided'}), 400

        # Создаем буфер для PDF
        buffer = BytesIO()
        
        # Инициализация документа
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=40,
            leftMargin=40,
            topMargin=40,
            bottomMargin=40
        )
        
        # Стили для текста
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            name='Center',
            alignment=TA_CENTER,
            fontSize=14,
            spaceAfter=20
        ))
        styles.add(ParagraphStyle(
            name='Body',
            alignment=TA_LEFT,
            fontSize=10,
            leading=14,
            spaceAfter=12
        ))
        styles.add(ParagraphStyle(
            name='Header',
            alignment=TA_LEFT,
            fontSize=12,
            textColor=colors.HexColor('#3B82F6'),
            spaceAfter=10
        ))
        styles.add(ParagraphStyle(
            name='Small',
            alignment=TA_LEFT,
            fontSize=8,
            textColor=colors.grey,
            spaceAfter=5
        ))

        # Элементы документа
        elements = []
        
        # Заголовок
        elements.append(Paragraph(
            f"Аналитический отчет: {report_data['channel_info']['title']}",
            styles['Center']
        ))
        
        # Период анализа
        elements.append(Paragraph(
            f"Период анализа: {report_data['analysis_period']['hours_back']} часов "
            f"({report_data['analysis_period']['start_time']} - {report_data['analysis_period']['end_time']})",
            styles['Small']
        ))
        elements.append(Spacer(1, 20))
        
        # Основные метрики
        metrics = [
            ['Метрика', 'Значение'],
            ['Подписчиков', report_data['channel_info']['subscribers']],
            ['Всего постов', report_data['summary']['total_posts']],
            ['Всего просмотров', report_data['summary']['total_views']],
            ['Средний охват', round(report_data['summary']['avg_views_per_post'], 1)],
            ['ER (просмотры)', f"{report_data['summary']['engagement_rate']['er_views']}%"],
            ['ER (подписчики)', f"{report_data['summary']['engagement_rate']['er_subscribers']}%"]
        ]
        
        metrics_table = Table(metrics, colWidths=[200, 100])
        metrics_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3F4F6')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1F2937')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB'))
        ]))
        elements.append(metrics_table)
        elements.append(Spacer(1, 30))
        
        # Рекомендации
        elements.append(Paragraph("Рекомендации", styles['Header']))
        for rec in report_data.get('recommendations', []):
            elements.append(Paragraph(f"• {rec}", styles['Body']))
        elements.append(Spacer(1, 20))
        
        # Анализ ИИ
        if ai_report:
            elements.append(Paragraph("ИИ Анализ", styles['Header']))
            for line in ai_report.split('\n'):
                if line.strip():
                    elements.append(Paragraph(line.strip(), styles['Body']))
            elements.append(Spacer(1, 20))
        
        # Топ постов
        if report_data.get('top_posts'):
            elements.append(Paragraph("Топ постов", styles['Header']))
            top_posts_data = [
                ['Дата', 'Просмотры', 'Тип', 'Превью']
            ]
            for post in report_data['top_posts']:
                preview = post['text_preview'][:50] + '...' if len(post['text_preview']) > 50 else post['text_preview']
                top_posts_data.append([
                    post['date'],
                    post['views'],
                    post['content_type'],
                    preview
                ])
            
            top_table = Table(top_posts_data, colWidths=[80, 60, 80, 200])
            top_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3F4F6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1F2937')),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB')),
                ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#E5E7EB'))
            ]))
            elements.append(top_table)
        
        # Создаем PDF
        doc.build(elements)
        
        # Подготовка ответа
        buffer.seek(0)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        return jsonify({
            'pdf_base64': base64.b64encode(pdf_data).decode('utf-8'),
            'filename': f"{report_data['channel_info']['title']}_report.pdf"
        })
    
    except Exception as e:
        logger.error(f"Ошибка генерации PDF: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# Отдача фронтенда
@app.route('/')
def home():
    return send_from_directory('static', 'index.html')
    
@app.route('/channel_subscribers', methods=['POST'])
def get_channel_subscribers():
    """Получение количества подписчиков"""
    try:
        data = request.get_json()
        channel_identifier = data.get('channel_username') or data.get('channel_id')
        
        if not channel_identifier:
            return jsonify({'error': 'Не указан username или ID канала'}), 400
        
        # Запускаем запрос в глобальном цикле
        future = asyncio.run_coroutine_threadsafe(
            analytics.get_channel_info(channel_identifier),
            loop
        )
        result = future.result(timeout=60)
        
        if result:
            return jsonify({
                'channel': result['title'],
                'username': result.get('username', ''),
                'subscribers': result.get('subscribers', 0),
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({'error': 'Канал не найден'}), 404
            
    except Exception as e:
        logger.error(f"Ошибка в channel_subscribers: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/find_channel', methods=['POST'])
def find_channel():
    """Поиск канала по названию"""
    try:
        data = request.get_json()
        query = data.get('query')
        
        if not query:
            return jsonify({'error': 'Не указан поисковый запрос'}), 400
        
        async def search():
            if not analytics.client or not analytics.client.is_connected():
                await analytics.init_client()
            
            results = []
            async for dialog in analytics.client.iter_dialogs():
                if query.lower() in dialog.name.lower():
                    results.append({
                        'id': dialog.id,
                        'title': dialog.name,
                        'username': dialog.entity.username,
                        'is_channel': dialog.is_channel
                    })
            return results
        
        # Запускаем поиск в глобальном цикле
        future = asyncio.run_coroutine_threadsafe(search(), loop)
        results = future.result(timeout=60)
        
        return jsonify({'results': results})
        
    except Exception as e:
        logger.error(f"Ошибка в find_channel: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    try:
        # Инициализация клиента Telegram
        logger.info("Инициализация Telegram клиента...")
        future = asyncio.run_coroutine_threadsafe(analytics.init_client(), loop)
        init_result = future.result(timeout=30)
        
        if not init_result:
            logger.error("Не удалось инициализировать Telegram клиент")
            sys.exit(1)
            
        # Проверка подключения к Supabase
        logger.info("Проверка подключения к Supabase...")
        try:
            supabase.table('ai_reports').select('*').limit(1).execute()
            logger.info("Подключение к Supabase успешно")
        except Exception as e:
            logger.error(f"Ошибка подключения к Supabase: {str(e)}")
            raise
        
        # Запуск Flask
        logger.info("Запуск Flask приложения...")
        app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Ошибка запуска приложения: {str(e)}", exc_info=True)
    finally:
        logger.info("Остановка цикла событий...")
        loop.call_soon_threadsafe(loop.stop)