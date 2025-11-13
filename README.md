UniFi AP Monitor & Maintenance Bot
Автоматизация обслуживания UniFi-точек доступа через UniFi Controller API:
плановые перезагрузки, RF-сканирование каналов, мониторинг состояния и уведомления в Telegram.

### Возможности

 - Плановая перезагрузка всех доступных AP по расписанию (раз в неделю, настраивается).

 - RF-сканирование каналов после ребута (аналог кнопки “Scan Channels” в UniFi GUI).

 - Мониторинг состояния AP — определяет переходы online/offline и сообщает о них.

 - Автоматическое “тихое окно” техобслуживания — во время ребута и сканирования мониторинг не шлёт уведомления.

 - Интеграция с Telegram — все отчёты и алерты отправляются в бот-чат.

 - Сохранение состояния AP в JSON (uptime, IP, модель, MAC, online-флаг).

 - Простая настройка через .env файл.

### Логика работы:

 - Скрипт подключается к UniFi Controller API.

 - Раз в неделю (или по команде) инициируется ребут всех онлайн-AP.

 - После 3-минутной паузы выполняется RF-сканирование для всех точек, которые уже поднялись.

 - Через REBOOT_WAIT_TIMEOUT (по умолчанию 10 минут) бот делает итоговый отчёт в Telegram:

 - ✅ Все AP онлайн

 - 🔴 Некоторые не вышли в онлайн

После завершения снимается флаг MAINTENANCE и возобновляется мониторинг.

### Пример .env файла
```
# UniFi Controller
UNIFI_HOST=192.168.1.1 
UNIFI_USER=admin
UNIFI_PASS=your_password
UNIFI_SITE=default

# Telegram
TELEGRAM_TOKEN=1234567890:ABCdefYourBotToken
TELEGRAM_CHAT_ID=

# Режимы
REBOOT_ENABLED=1
REBOOT_DOW=sat           #mon, thu, wed, tue, fri, sat, sun
REBOOT_AT=23:00
REBOOT_WAIT_TIMEOUT=600  # ожидание после ребута (сек)

# Мониторинг
POLL_INTERVAL=20         # время опроса в секундах
STATE_FILE=unifi_ap_state.json # Информация о состоянии AP
```

