import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from pymongo import MongoClient
import logging
from datetime import datetime
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
PRODUCTION = os.getenv('PRODUCTION')

mongodb_uri = os.getenv('MONGODB_URI')

# Ensure the URI is available
if not mongodb_uri:
    raise ValueError("MONGODB_URI is not set in the environment")

# Connect to MongoDB
client = MongoClient(mongodb_uri)

# Access the database (replace 'appdb' with your database name if needed)
db = client.get_database()  # This will use the database specified in the URI

# Example usage: Access a collection and perform an operation
users_collection = db['users']


# Функция для добавления пользователя в базу данных, если его нет
def add_user_if_not_exists(chat_id):
    existing_user = users_collection.find_one({'chat_id': chat_id})
    if not existing_user:
        users_collection.insert_one({'chat_id': chat_id, 'followed_products': []})

def follow_product(chat_id, product_id, name, price, size=None):
    add_user_if_not_exists(chat_id)
    users_collection.update_one(
        {'chat_id': chat_id},
        {'$addToSet': {'followed_products': {
            'product_id': product_id,
            'name': name,
            'lastprice': price,
            'size': size,
            'previous_price': price,
            'has_changed': False,
            'last_updated': datetime.now()
        }}}
    )

# Функция для удаления товара из массива followed_products
def unfollow_product(chat_id, product_id):
    users_collection.update_one(
        {'chat_id': chat_id},
        {'$pull': {'followed_products': {'product_id': product_id}}}
    )

# Функция для получения всех пользователей
def get_all_users():
    return users_collection.find()

# Функция для получения списка товаров, на которые подписан пользователь
def get_user_products(chat_id):
    user = users_collection.find_one({'chat_id': chat_id})
    if user:
        return user['followed_products']
    return []


def update_product_data():
    print('Обновление данных о продуктах')
    all_users = get_all_users()
    unique_product_ids = set()

    for user in all_users:
        followed_products = user.get('followed_products', [])
        for product in followed_products:
            unique_product_ids.add(product['product_id'])

    if not unique_product_ids:
        return

    # Формируем запрос с артикулами
    article_list = ';'.join(map(str, unique_product_ids))
    url = f'https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&locale=ru&spp=30&lang=ru&ab_testing=false&nm={article_list}'
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        products_data = data['data']['products']

        # Обновляем данные о товарах для всех пользователей
        for product_data in products_data:
            product_id = product_data['id']
            name = product_data['name']

            # Итерируем по пользователям, которые отслеживают данный товар
            users = users_collection.find({'followed_products.product_id': product_id})
            for user in users:
                for followed_product in user['followed_products']:
                    if followed_product['product_id'] == product_id:
                        size_to_track = followed_product.get('size')
                        old_price = followed_product['lastprice']
                        has_changed = followed_product.get('has_changed', False)

                        # Если размер указан, ищем цену конкретного размера
                        if size_to_track:
                            # Ищем цену для выбранного размера
                            new_price = None
                            for size in product_data['sizes']:
                                if size['origName'] == size_to_track:
                                    new_price = size['price']['total'] / 100
                                    break

                            if new_price is None:
                                print(f"Размер {size_to_track} не найден для товара {name}. Пропускаем.")
                                continue
                        else:
                            # Если размер не указан, берем цену первого размера
                            new_price = product_data['sizes'][0]['price']['total'] / 100

                        # Проверяем изменение цены
                        if new_price != old_price:
                            has_changed = True
                            price_difference = new_price - old_price
                            percentage_change = (price_difference / old_price) * 100
                        else:
                            has_changed = False

                        # Обновляем товар для пользователя
                        users_collection.update_one(
                            {'chat_id': user['chat_id'], 'followed_products.product_id': product_id},
                            {'$set': {
                                'followed_products.$.name': name,
                                'followed_products.$.lastprice': new_price,
                                'followed_products.$.previous_price': old_price,  # Сохраняем старую цену
                                'followed_products.$.has_changed': has_changed,
                                'followed_products.$.last_updated': datetime.now()
                            }}
                        )
    except requests.RequestException as e:
        logging.error(f"Ошибка при обновлении данных о товарах: {e}")
        

# Функция для отправки обновлений пользователям
async def send_update_to_users(context: ContextTypes.DEFAULT_TYPE):
    update_product_data()
    all_users = get_all_users()

    for user in all_users:
        chat_id = user['chat_id']
        followed_products = user.get('followed_products', [])

        if followed_products:
            messages = []
            for product in followed_products:
                if product.get('has_changed', False):
                    product_id = product['product_id']
                    name = product['name']
                    last_price = product['lastprice']
                    previous_price = product.get('previous_price', last_price)

                    # Рассчитываем изменение цены
                    price_diff = last_price - previous_price
                    price_diff_percent = (price_diff / previous_price) * 100 if previous_price > 0 else 0
                    change_direction = "выросла" if price_diff > 0 else "упала"
                    
                    messages.append(f'Товар: {name}\n'
                                    f'Новая цена: {last_price} руб.\n'
                                    f'Цена {change_direction} на {abs(price_diff)} руб. ({abs(price_diff_percent):.2f}%)\n'
                                    f'Ссылка: https://www.wildberries.ru/catalog/{product_id}/detail.aspx')

                    # Сброс флага has_changed и обновление previous_price
                    users_collection.update_one(
                        {'chat_id': chat_id, 'followed_products.product_id': product['product_id']},
                        {'$set': {'followed_products.$.has_changed': False,
                                  'followed_products.$.previous_price': last_price}}  # Обновляем previous_price
                    )

            if messages:
                message = '\n\n'.join(messages)
                await context.bot.send_message(chat_id=chat_id, text=message, disable_web_page_preview=True)

def generate_url(articles):
    if articles:
        articles_str = ';'.join(map(str, articles))
        return f'https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&locale=ru&spp=30&lang=ru&ab_testing=false&nm={articles_str}'
    return None

def extract_product_data(data):
    products = data.get('data', {}).get('products', [])
    product_list = []
    for product in products:
        try:
            name = product['name']
            price = product['sizes'][0]['price']['total'] / 100
            product_list.append({'name': name, 'price': price})
        except (KeyError, IndexError) as e:
            print(f"Ошибка при извлечении данных о товаре: {e}")
    return product_list

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Добро пожаловать! Этот бот поможет вам отслеживать изменения цен на товары на Wildberries и получать уведомления о снижении или повышении цен.\n /help - руководство пользователя')

async def how(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = []
    messages.append('**Как работает бот:**\n\n'
                    'Каждый день в 2 часа дня бот проверяет обновления цен на товары, которые вы отслеживаете. Если цена изменилась, вы получите уведомление с информацией о том, как изменилась цена (выросла или упала), на сколько рублей и процентов.\n\n'
                    'Следите за своими товарами легко и не пропускайте выгодные предложения!'
                    )
    message = '\n\n'.join(messages)
    await update.message.reply_text(message, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = []
    messages.append('**Основные команды:**\n\n'
                    '/help\n'
                    'Показывает список доступных команд\n\n'

                    '/follow <артикул товара>\n'
                    'Добавляет товар для отслеживания по артикулу.\n' 
                    'Пример:\n'
                    '```\n/follow 12345678\n```\n'

                    '/unfollow <артикул товара>\n'
                    'Удаляет товар из списка отслеживаемых.\n' 
                    'Пример:\n'
                    '```\n/unfollow 12345678\n```\n'
                
                    '/clear\n'
                    'Полностью очищает список всех товаров, которые вы отслеживаете.\n\n'
                
                    '/check\n'
                    'Показывает текущий список отслеживаемых товаров с их ценами.\n\n'

                    '/how\n'
                    'Краткое описание того, как работает этот бот.'
                )
    
    message = '\n\n'.join(messages)
    await update.message.reply_text(message, parse_mode="Markdown")


async def follow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            article_number = int(context.args[0])

            # Формируем запрос на получение информации о товаре
            url = f'https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&locale=ru&spp=30&lang=ru&ab_testing=false&nm={article_number}'
            response = requests.get(url)
            data = response.json()

            try:
                product = data['data']['products'][0]
                name = product['name']
                sizes = product.get('sizes', [])

                # Проверяем, есть ли у товара несколько размеров
                if len(sizes) > 1:
                    # Отправляем пользователю выбор размеров
                    size_options = [f"{size['origName']}" for size in sizes]
                    size_keyboard = [[option] for option in size_options]
                    reply_markup = ReplyKeyboardMarkup(size_keyboard, one_time_keyboard=True)
                    
                    # Сохраняем артикул и размеры в контексте для дальнейшей обработки
                    context.user_data['article_number'] = article_number
                    context.user_data['name'] = name
                    context.user_data['sizes'] = sizes
                    context.user_data['awaiting_size_selection'] = True  # Устанавливаем флаг для ожидания выбора размера
                    
                    await update.message.reply_text(
                        'Этот товар доступен в нескольких размерах. Пожалуйста, выберите размер для отслеживания:',
                        reply_markup=reply_markup
                    )
                else:
                    # Если размер один, сразу добавляем товар
                    price = sizes[0]['price']['total'] / 100
                    follow_product(update.message.chat_id, article_number, name, price)
                    await update.message.reply_text(f'Артикул {article_number} добавлен в список.')

            except (KeyError, IndexError) as e:
                print(f"Ошибка при извлечении данных о товаре: {e}")
                await update.message.reply_text('Ошибка при получении данных о товаре.')
                
        except ValueError:
            await update.message.reply_text('Пожалуйста, введите действительный артикул товара.')
    else:
        await update.message.reply_text('Пожалуйста, укажите артикул товара.')

# Обработчик для выбора размера
async def handle_size_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get('awaiting_size_selection'):
        size_selected = update.message.text
        sizes = context.user_data.get('sizes', [])
        article_number = context.user_data.get('article_number')
        name = context.user_data.get('name')

        # Ищем выбранный размер среди доступных
        selected_size = next((size for size in sizes if size_selected.startswith(size['origName'])), None)
        print(selected_size)

        if selected_size:
            sizeName = f"{selected_size['origName']}"
            try:
                price = selected_size['price']['total'] / 100
                follow_product(update.message.chat_id, article_number, name, price, size=sizeName)
                await update.message.reply_text(f'{name} с размером {sizeName} добавлен в список отслеживаемых.')
            except:
                await update.message.reply_text(f'{name} с размером {sizeName} нет в наличии.')
        else:
            await update.message.reply_text('Выбранный размер не найден.')
        
        # Сбрасываем флаг ожидания выбора размера
        context.user_data['awaiting_size_selection'] = False

async def unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            article_number = int(context.args[0])
            unfollow_product(update.message.chat_id, article_number)
            await update.message.reply_text(f'Артикул {article_number} удален из списка.')
        except ValueError:
            await update.message.reply_text('Пожалуйста, введите действительное числовое значение.')
    else:
        await update.message.reply_text('Пожалуйста, укажите артикул товара для удаления.')

# Функция для очистки всех товаров пользователя
def clear_followed_products(chat_id):
    users_collection.update_one(
        {'chat_id': chat_id},
        {'$set': {'followed_products': []}}  # Очищаем массив товаров
    )

# Обработчик команды /clear
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    clear_followed_products(chat_id)
    await update.message.reply_text('Ваш список товаров был успешно очищен.')

# Функция для получения информации о текущих отслеживаемых товарах пользователя
async def check_followed_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user = users_collection.find_one({'chat_id': chat_id})
    
    if not user or not user.get('followed_products'):
        await update.message.reply_text('У вас нет отслеживаемых товаров.')
        return
    
    followed_products = user['followed_products']
    
    # Формируем сообщение с информацией о каждом отслеживаемом товаре
    messages = []
    for product in followed_products:
        product_id = product['product_id']
        name = product['name']
        size = product.get('size', None)
        price = product['lastprice']
    
        messages.append(f'Артикул: {product_id}.\n'
                        f'Товар: {name}\n'
                        f'Размер: {size}\n'
                        f'Цена: {price} руб.\n'
                        f'Ссылка: https://www.wildberries.ru/catalog/{product_id}/detail.aspx')
        
    message = '\n\n'.join(messages)
    await update.message.reply_text(message, disable_web_page_preview=True)

def main() -> None:
    # Создаем объект Application и передаем ему токен
    application = Application.builder().token(TOKEN).build()
    print('Запускаем Бота')

    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()

    if PRODUCTION == True:
        # Каждый день в 11 утра
        scheduler.add_job(send_update_to_users, trigger=CronTrigger(hour=11, minute=0), args=[application])
    else:
        # Каждую минуту начиная с запуска бота
        scheduler.add_job(send_update_to_users, trigger=IntervalTrigger(minutes=1), args=[application])
    
    scheduler.start()
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("how", how))
    application.add_handler(CommandHandler("follow", follow))
    application.add_handler(CommandHandler("unfollow", unfollow))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("check", check_followed_products))

    # Обработчик для текстовых сообщений (для обработки выбора размера)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_size_selection))

    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()
