# Обновление системы
pkg update -y && pkg upgrade -y

# Установка Python и Git
pkg install -y python git

# Установка pip и зависимостей
pip install --upgrade pip
pip install telethon requests

# Скачивание и запуск бота
cd ~
curl -O https://raw.githubusercontent.com/p0slv/giftopenbuy/refs/heads/main/main.py
python main.py
