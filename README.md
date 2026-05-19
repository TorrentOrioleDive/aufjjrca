# nekto-audiocall-mitm

MITM-ретрансляция голосового чата [nekto.me/audiochat](https://nekto.me/audiochat) на 2 токена. Идея та же, что и у текстового [pashtetx/nekto.me-spion](https://github.com/pashtetx/nekto.me-spion), но для звонков:

```
Случайный X  <──audio──>  Бот A (token_1)   ─── relay ───   Бот B (token_2)  <──audio──>  Случайный Y
```

Каждый бот ищет случайного собеседника в обычном голосовом чате nekto.me и устанавливает с ним обычное WebRTC P2P. Между ботами голос проксируется внутри процесса: что говорит X — слышит Y, и наоборот. Они не подозревают друг о друге.

## Дисклеймер

Это reverse-engineering неофициального API. Никаких гарантий совместимости, стабильности, легальности использования по ToS nekto.me — на твоей совести. Не используй для harassment'а, scam'а и прочей мерзости. Никаких политических звонков, разводов на деньги — иначе тебя забанят, а карму потом не отмыть.

## Требования

* Python 3.10+
* `ffmpeg` / системные библиотеки PortAudio для `aiortc` (на Ubuntu: `sudo apt install ffmpeg libavdevice-dev libavfilter-dev libopus-dev libvpx-dev pkg-config`)
* 2 валидных токена nekto.me + User-Agent браузера, в котором они получены

## Установка

```bash
git clone https://github.com/Grottobridolmen/nekto.git
cd nekto
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.ini config.ini
# открой config.ini, вставь свои 2 токена и UA, см. ниже
```

## Откуда взять токен

1. Открой [https://nekto.me/audiochat](https://nekto.me/audiochat) в **инкогнито**.
2. Прими cookies, дай микрофон, сделай 1–2 коротких реальных звонка (прогрев против антибота — иначе токен может быть помечен, и первое же подключение из бота резко обрывается).
3. Открой DevTools → Console и выполни:
   ```js
   JSON.parse(localStorage.getItem("storage_audio_v2")).user.authToken
   ```
   — это твой `token` (36-символьный UUID с дефисами).

   > Старый ключ `localStorage.getItem("audio-chat-uid")` сейчас возвращает `null` — nekto переехал на `storage_audio_v2` с вложенным `user.authToken`. Если в твоей версии сайта схема снова поменяется — посмотри ключи через `Object.keys(localStorage)` и поищи поле, похожее на UUID.
4. Запиши `navigator.userAgent` (это твой `ua`) — он будет использован при подключении из бота.

Сделай это **дважды**, в двух разных инкогнито-окнах (можно с разными прокси / устройств), чтобы получить два независимых токена.

## Конфиг

См. [`config.example.ini`](./config.example.ini). Минимально:

```ini
[settings]
clients = bot_a bot_b
debug = False

[client/bot_a]
token = <первый_токен>
ua    = <UA_браузера_1>
my-sex   = M
my-age   = 18,21
wish-sex = F
wish-age = 18,21

[client/bot_b]
token = <второй_токен>
ua    = <UA_браузера_2>
my-sex   = F
my-age   = 18,21
wish-sex = M
wish-age = 18,21
```

Поля `my-sex / wish-sex / my-age / wish-age` едут в `searchCriteria` запроса `scan-for-peer`. Можно оставить любые поля пустыми — это означает "любой".

## Запуск

```bash
python main.py
```

Что будет в логах:

```
13:42:01 info     startup                       bots=['bot_a', 'bot_b']
13:42:01 info     ws.connect                    url=wss://audiochat.nekto.me/audiochat/ws/chat/ bot=bot_a
13:42:01 info     registered                    bot=bot_a token_id=... connection_id=...
13:42:01 info     search.start                  bot=bot_a criteria={...}
13:42:08 info     peer-connect                  bot=bot_a connection_id=... initiator=True
13:42:08 info     rtc.offer.sent                bot=bot_a
13:42:09 info     rtc.ice                       bot=bot_a state=connected
13:42:09 info     rtc.track                     bot=bot_a kind=audio
13:42:09 info     bridge.inbound                side=a kind=audio
13:42:09 info     rtc.outbound.swapped_to_relay bot=bot_b
13:42:10 info     rtc.outbound.swapped_to_relay bot=bot_a
```

С момента появления обоих `bridge.inbound` (`side=a` и `side=b`) X и Y слышат друг друга.

## Архитектура

```
nekto/
├── __init__.py
├── __main__.py        # python -m nekto
├── config.py          # ini → ClientSpec
├── signaling.py       # WebSocket + JSON dispatch
├── client.py          # NektoAudioClient (WebRTC + signaling lifecycle)
├── bridge.py          # MitmBridge — кросс-сессионный аудио-мост
├── fingerprint.py     # минимальный set-fpt payload
├── challenge.py       # btoa-fallback challenge-{proof,trace,ack}
└── util.py
main.py
config.example.ini
requirements.txt
```

### Что реверсилось из app.js

* Endpoint: `wss://audiochat.nekto.me/audiochat/ws/chat/`
* Auth: `register { userId: token, version: 24, isTouch, messengerNeedAuth, timeZone, locale }`
* `set-fpt` отправляется с `infoData` (fallback на plain JSON; шифрование `infoDataS` мы намеренно не реализуем)
* `scan-for-peer { peerToPeer: true, searchCriteria, token: null }`
* `peer-connect { connectionId, turnParams, relay, stunUrl, initiator, createTime }`
* WebRTC: `offer / answer / ice-candidate` — payload `JSON.stringify`'нут внутрь поля `offer / answer / candidate`
* Antibot challenge: `challenge-proof / challenge-trace / challenge-ack` отвечаются с `checksum = btoa(seed:bucket:stamp).slice(0,48)` (fallback-путь из приложения)

## Known issues / TODO

* **Antibot AES-GCM**: реальный клиент шифрует `set-fpt.infoDataS` и `challenge.checksum` через AES-GCM с ключом, производным от `visitorId + tokenId`. Мы пользуемся открытым fallback'ом (`infoData`, btoa-checksum). Если nekto начнёт отбрасывать такие сообщения — нужно реализовать симметричную шифровку.
* **ICE trickle**: `aiortc` собирает кандидаты в SDP, отдельный поток `ice-candidate` не шлёт. Большинство ICE кейсов работают, но если получается дрянная связность — добавить трикл вручную через `aioice.Connection`.
* **Fingerprint реалистичность**: `components` упрощён. Если nekto начнёт банить — добавить плагины, шрифты, GPU info, real canvas hash.
* **Подбор пары X/Y**: nekto матчит "сначала пришёл — первого получил". Если боты ищут с одинаковыми критериями, есть ненулевой шанс что они подберутся друг другу — тогда оба окажутся в звонке между собой. Это не ломает программу, но MITM-смысла в этом нет; либо разнеси критерии (см. `my-sex` отличающиеся), либо реализуй back-off + retry если `peer-connect` приходит между ботами на одном `connectionId`.
* **Качество звука**: внутри bridge'а идёт реcжатие Opus → PCM → Opus, потому что aiortc не умеет relay в bypass-режиме. Качество в норме, но добавит ~40 мс RTT.

## Лицензия

MIT. См. [`LICENSE`](./LICENSE).
