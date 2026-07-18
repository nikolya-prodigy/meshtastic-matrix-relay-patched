# MMRelay Patched

## Meshtastic <=> Matrix с управляемыми комнатами

MMRelay Patched — двусторонний мост между Meshtastic и Matrix. Он подключается
к обычной Meshtastic-ноде по TCP, Serial или BLE и переносит сообщения между
mesh-каналами, личными сообщениями Meshtastic и комнатами Matrix.

Проект является форком
[MMRelay](https://github.com/jeremiah-k/meshtastic-matrix-relay) от Jeremiah
Kellogg. Исходный MMRelay остаётся первоисточником базового моста, плагинной
системы, E2EE, базы данных и поддержки способов подключения. Этот форк
синхронизируется с `upstream/main`, но содержит отдельный набор изменений для
эксплуатации на собственном Matrix-сервере.

> Это не Matrix Application Service. Комнаты создаёт и обслуживает обычный
> Matrix-пользователь, под которым запущен бот.

## Чем отличается форк

| Возможность            | MMRelay upstream                    | Этот форк                                                                             |
| ---------------------- | ----------------------------------- | ------------------------------------------------------------------------------------- |
| Связь каналов и комнат | Статический список `matrix_rooms`   | Автоматически создаваемые Space и комнаты каналов                                     |
| Личные сообщения       | Ручная привязка                     | Отдельная DM-комната для каждой Meshtastic-ноды                                       |
| Управление             | Команды плагинов в обычных комнатах | Приватная control-room с командами состояния и диагностики                            |
| Вид входящих сообщений | Настраиваемый текстовый префикс     | Короткое и полное имя, текст и отдельная строка с параметрами радиолинка              |
| Длинные сообщения      | Обрезаются до лимита Meshtastic     | Делятся на UTF-8-безопасные части с настраиваемой паузой                              |
| Статус отправки        | Логи моста                          | Реакции Matrix: отправлено, ACK, NAK или тайм-аут                                     |
| Ping                   | Команда `!ping`                     | Дополнительно автоматический ответ на заданные слова с параметрами входящего линка    |
| Погода                 | Плагин внешнего погодного сервиса   | Внешняя погода удалена; `weather` показывает только телеметрию локальных датчиков нод |

При этом сохранены возможности upstream: двусторонний relay, Matrix E2EE,
нативные replies и reactions, SQLite, плагины, MQTT-пакеты и подключения к
Meshtastic по TCP, Serial и BLE.

## Управляемые комнаты

При включённом `meshtastic_portals` бот поддерживает следующую структуру:

- **Meshtastic Space** — объединяет все комнаты моста.
- **Комнаты каналов** — создаются для настроенных на локальной ноде каналов.
- **DM-комнаты** — создаются при первом входящем личном сообщении или командой
  `dm <number|node-id|name>`.
- **Control-room** — приватная комната для управления мостом.

Мост восстанавливает привязки ранее созданных DM-комнат после перезапуска,
обновляет названия и topics комнат, приглашает заданных пользователей и может
установить одну иконку для бота, Space и портальных комнат.

Пользователей можно приглашать в комнаты только для чтения. Если параметр
`meshtastic_portals.access.channel_writers` задан, отправлять сообщения из
комнат каналов в mesh смогут только перечисленные Matrix ID. Отсутствие этого
параметра разрешает запись всем участникам, а пустой список запрещает её всем.

## Формат сообщений

В портальной Matrix-комнате входящее сообщение выглядит так:

```text
NICK Полное имя ноды
Текст сообщения

link: LoRa, 2 hops, SNR -18.8 dB, RSSI -110 dBm, relay No5g #16
```

Для пакета, пришедшего через MQTT, строка будет `link: MQTT`. Короткое имя
выделяется отдельно, полное имя помогает различать ноды с одинаковым
`shortName`, а пустая строка отделяет текст от технической информации.

Форк передаёт нативные Meshtastic replies и emoji reactions в обе стороны.
Чтобы ответы и реакции продолжали работать после рестарта, необходимо оставить
`database.msg_map.wipe_on_restart: false`.

## Control-room

Команда `help` выводит актуальный список. Основные команды:

| Команда                                                       | Назначение                                                        |
| ------------------------------------------------------------- | ----------------------------------------------------------------- |
| `health`                                                      | Краткое состояние mesh-сети                                       |
| `nodes [online\|limit\|all]`                                  | Список известных нод с линком и временем последнего появления     |
| `find <query>`                                                | Поиск нод и формирование короткого нумерованного списка           |
| `node <number\|node-id\|name>`                                | Полная карточка одной ноды                                        |
| `signal <number\|node-id\|name>`                              | Качество последнего радиолинка                                    |
| `trace <number\|node-id\|name>`                               | Traceroute до ноды                                                |
| `telemetry <target> [device\|environment\|air\|power\|local]` | Запрос телеметрии                                                 |
| `weather [<number\|node-id\|name>]`                           | Последние показания environment-датчиков, без запросов в интернет |
| `dm <number\|node-id\|name>`                                  | Создать или открыть DM-комнату ноды                               |
| `channels` / `rooms`                                          | Показать каналы Meshtastic или комнаты Matrix                     |
| `status` / `queue` / `sent [limit]`                           | Состояние моста и очереди отправки                                |
| `writers`                                                     | Пользователи с правом отправки в каналы                           |
| `refresh`                                                     | Обновить комнаты, профили и avatar бота                           |
| `map`                                                         | Карта нод с известными координатами                               |
| `battery` / `voltage` / `air`                                 | Графики сохранённой телеметрии                                    |

Запросы traceroute и telemetry выполняются в фоне, поэтому control-room не
блокируется на время ожидания ответа от mesh.

## Развёртывание через Ansible

Для роли Meshtastic в
[`matrix-docker-ansible-deploy`](https://github.com/spantaleev/matrix-docker-ansible-deploy)
используются актуальные переменные с префиксом
`matrix_bridge_meshtastic_relay_*`. Старый префикс
`matrix_meshtastic_relay_*` больше использовать не следует.

Минимальная основа для `inventory/host_vars/<matrix-domain>/vars.yml`:

```yaml
matrix_bridge_meshtastic_relay_enabled: true
matrix_bridge_meshtastic_relay_container_image: >-
  ghcr.io/nikolya-prodigy/mmrelay-patched:<image-tag>

matrix_bridge_meshtastic_relay_matrix_host: "{{ matrix_domain }}"
matrix_bridge_meshtastic_relay_matrix_bot_password: "{{ vault_meshtastic_bot_password }}"

matrix_bridge_meshtastic_relay_connection_type: tcp
matrix_bridge_meshtastic_relay_tcp_host: "192.0.2.10"
matrix_bridge_meshtastic_relay_meshnet_name: "My Mesh"

# В managed-режиме статические привязки комнат не нужны.
matrix_bridge_meshtastic_relay_matrix_rooms_list: []

matrix_bridge_meshtastic_relay_configuration_extension_yaml: |
  # Конфигурация форка из следующего раздела.
```

Пароль бота лучше хранить через Ansible Vault. Готовые образы ветки
`patch/bot-managed-portals` публикуются в
`ghcr.io/nikolya-prodigy/mmrelay-patched`; тег содержит версию, имя ветки и
короткий SHA коммита.

## Новые параметры форка

Все дополнительные параметры передаются внутри
`matrix_bridge_meshtastic_relay_configuration_extension_yaml`. Ниже приведён
полный рекомендуемый пример; Matrix ID, aliases и URL иконки нужно заменить на
свои.

```yaml
matrix_bridge_meshtastic_relay_configuration_extension_yaml: |
  meshtastic_portals:
    enabled: true
    alias_prefix: meshtastic

    invite_users:
      - "@admin:{{ matrix_domain }}"

    access:
      channel_writers:
        - "@admin:{{ matrix_domain }}"

    icon:
      # Допускается https:// URL или уже загруженный mxc:// URI.
      url: "https://example.org/meshtastic.png"
      bot: true
      space: true

    control:
      enabled: true
      users:
        - "@admin:{{ matrix_domain }}"
      room_name: "Meshtastic bot"
      alias: meshtastic-control
      send_welcome_on_start: false
      allow_commands_in_portal_rooms: false

    space:
      enabled: true
      name: Meshtastic
      alias: meshtastic-space

    channels:
      auto_create: true
      include_empty: false
      # Доступны {index} и {name}.
      name_template: "#{index} {name}"

    direct_messages:
      auto_create: true
      # Доступны {name}, {short_name}, {long_name} и {node_id}.
      name_template: "DM {long_name}"

  matrix:
    # Используется для legacy/static rooms. У portal-сообщений свой формат.
    prefix_enabled: true
    prefix_format: "{short}: "

  meshtastic:
    message_interactions:
      reactions: true
      replies: true

    delivery_receipts:
      enabled: true
      request_ack: true
      timeout_secs: 60
      reactions:
        sent: "📡"
        ack: "✅"
        nak: "❌"
        timeout: "⌛"

    message_fragmentation:
      enabled: true
      # Размер считается в UTF-8 байтах и ограничивается максимумом 233.
      max_payload_bytes: 200
      fragment_delay_secs: 15
      # В обоих шаблонах доступны {index} и {total}.
      prefix_template: "[{index}/{total}] "
      last_suffix_template: ""

    # Не расходовать эфир на Matrix display-name перед каждым сообщением.
    prefix_enabled: false

  plugins:
    ping:
      active: true
      auto_pong:
        enabled: true
        # Совпадение всего сообщения, без учёта регистра.
        triggers: ["ping", "пинг", "test", "тест", "проверка"]
        # all или список номеров каналов, например [0, 2].
        channels: all
        response: "autopong"
        include_link_details: true

  database:
    msg_map:
      wipe_on_restart: false
```

### Краткий справочник

| Параметр                           | Что меняет                                                         |
| ---------------------------------- | ------------------------------------------------------------------ |
| `meshtastic_portals.enabled`       | Включает управляемые ботом Space, channel rooms, DM и control-room |
| `alias_prefix`                     | Префикс автоматически создаваемых room aliases                     |
| `invite_users`                     | Matrix ID для автоматического приглашения в порталы                |
| `access.channel_writers`           | Разрешённые отправители Matrix -> публичные Meshtastic-каналы      |
| `icon.*`                           | Источник и область применения Matrix avatar                        |
| `control.*`                        | Доступ, имя, alias и область работы control-команд                 |
| `space.*`                          | Автоматический Space и его профиль                                 |
| `channels.*`                       | Создание комнат каналов и шаблон их имён                           |
| `direct_messages.*`                | Автосоздание DM и шаблон имени комнаты                             |
| `message_interactions.*`           | Нативный relay replies и reactions                                 |
| `delivery_receipts.*`              | Запрос ACK и Matrix-реакции состояния доставки                     |
| `message_fragmentation.*`          | Деление длинного текста и pacing частей                            |
| `plugins.ping.auto_pong.*`         | Триггеры, ответ, каналы и параметры линка в auto-pong              |
| `database.msg_map.wipe_on_restart` | Сохранение связей событий для replies/reactions после рестарта     |

## Документация upstream

Общие способы установки, Matrix E2EE, плагины и базовая настройка MMRelay
описаны в первоисточнике:

- [Installation Instructions](docs/INSTRUCTIONS.md)
- [Docker Guide](docs/DOCKER.md)
- [Kubernetes Guide](docs/KUBERNETES.md)
- [E2EE Setup Guide](docs/E2EE.md)
- [Advanced Configuration](docs/ADVANCED_CONFIGURATION.md)
- [MMRelay Wiki](https://github.com/jeremiah-k/meshtastic-matrix-relay/wiki)

Лицензия проекта: [GPL-3.0-or-later](LICENSE).
