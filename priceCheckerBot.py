import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
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

# Вставьте свой токен сюда
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Подключение к MongoDB
client = MongoClient('mongodb://localhost:27017/')
db = client['telegram_bot_db']
users_collection = db['users']
# Функция для добавления пользователя в базу данных, если его нет
def add_user_if_not_exists(chat_id):
    existing_user = users_collection.find_one({'chat_id': chat_id})
    if not existing_user:
        users_collection.insert_one({'chat_id': chat_id, 'followed_products': []})

# Функция для добавления товара в массив followed_products пользователя
def follow_product(chat_id, product_id, name, price):
    add_user_if_not_exists(chat_id)
    users_collection.update_one(
        {'chat_id': chat_id},
        {'$addToSet': {'followed_products': {
            'product_id': product_id, 
            'name': name, 
            'lastprice': price, 
            'has_changed': False,  # Инициализируем флаг has_changed как false
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

# Функция для обновления данных о товарах
def update_product_data():
    print('Обновление данных о продуктах')
    # Получаем все уникальные артикулы, на которые подписаны пользователи
    all_users = get_all_users()
    unique_product_ids = set()

    for user in all_users:
        followed_products = user.get('followed_products', [])
        for product in followed_products:
            unique_product_ids.add(product['product_id'])

    # Если нет товаров для обновления, ничего не делаем
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
            new_price = product_data['sizes'][0]['price']['total'] / 100

            # Обновляем информацию о товаре у всех пользователей, проверяя изменения цены
            users = users_collection.find({'followed_products.product_id': product_id})
            for user in users:
                for followed_product in user['followed_products']:
                    if followed_product['product_id'] == product_id:
                        old_price = followed_product['lastprice']
                        has_changed = followed_product.get('has_changed', False)

                        # Проверяем, изменилась ли цена
                        if new_price != old_price:
                            has_changed = True
                        else:
                            has_changed = False

                        # Обновляем товар для пользователя
                        users_collection.update_one(
                            {'chat_id': user['chat_id'], 'followed_products.product_id': product_id},
                            {'$set': {
                                'followed_products.$.name': name, 
                                'followed_products.$.lastprice': new_price,
                                'followed_products.$.has_changed': has_changed,
                                'followed_products.$.last_updated': datetime.now()
                            }}
                        )
    except requests.RequestException as e:
        logging.error(f"Ошибка при обновлении данных о товарах: {e}")


async def send_update_to_users(context: ContextTypes.DEFAULT_TYPE):
    update_product_data()
    all_users = get_all_users()
    for user in all_users:
        chat_id = user['chat_id']
        print(f'Отправляю рассылку ${chat_id}')
        followed_products = user.get('followed_products', [])
        
        if followed_products:
            messages = []
            for product in followed_products:
                # if product.get('has_changed', False):
                    name = product['name']
                    last_price = product['lastprice']
                    messages.append(f'Товар: {name}\nНовая цена: {last_price} руб.')
                    
                    # Сброс флага has_changed после отправки сообщения
                    users_collection.update_one(
                        {'chat_id': chat_id, 'followed_products.product_id': product['product_id']},
                        {'$set': {'followed_products.$.has_changed': False}}
                    )
            
            if messages:
                message = '\n\n'.join(messages)
                await context.bot.send_message(chat_id=chat_id, text=message)

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
    await update.message.reply_text('Привет! Я могу помочь отслеживать цены на товары на WB. Я не дам тебе пропустить изменение цены!')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Для того чтобы добавить новый товар для отслеживания используйте: /follow <артикул товара>\nУдалить Товар из отслеживания: /unfollow <артикул товара>')


async def follow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            article_number = int(context.args[0])

            url = f'https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&locale=ru&spp=30&lang=ru&ab_testing=false&nm={article_number}'
            response = requests.get(url)
            data = response.json()
            
            try:
                product = data['data']['products'][0]
                name = product['name']
                price = product['sizes'][0]['price']['total'] / 100
                follow_product(update.message.chat_id, article_number, name, price)
                await update.message.reply_text(f'Артикул {article_number} добавлен в список.')
            except (KeyError, IndexError) as e:
                print(f"Ошибка при извлечении данных о товаре: {e}")

            # Добавляем товар и пользователя в базу данных
        except ValueError:
            await update.message.reply_text('Пожалуйста, введите действительный артикул товара.')
    else:
        await update.message.reply_text('Пожалуйста, укажите артикул товара.')


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
        price = product['lastprice']
    
        messages.append(f'Артикул: {product_id}.\nТовар: {name}\nЦена: {price} руб.')
    
    message = '\n\n'.join(messages)
    await update.message.reply_text(message)

def main() -> None:
    # Создаем объект Application и передаем ему токен
    application = Application.builder().token(TOKEN).build()
    print('Запускаем Бота')

    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()

    # Каждые 24 часа начиная с запуска бота
    scheduler.add_job(send_update_to_users, trigger=IntervalTrigger(minutes=1), args=[application])

    # Каждый день в 11 утра
    # scheduler.add_job(send_update_to_users, trigger=CronTrigger(hour=11, minute=0), args=[application])
    scheduler.start()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("follow", follow))
    application.add_handler(CommandHandler("unfollow", unfollow))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("check", check_followed_products))

    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()
