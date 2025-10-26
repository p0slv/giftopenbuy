# Обновление системы
pkg update && pkg upgrade

# Установка Python и Git
pkg install python git

# Установка pip и зависимостей
pip install --upgrade pip
pip install telethon requests

# Скачивание и запуск бота
cd ~
rm main.py
curl -O https://raw.githubusercontent.com/p0slv/giftopenbuy/refs/heads/main/main.py
python main.py
