# Протокол публикации Хабра (внутренний API `kek/v2`)

> Реверс-инжиниринг на основе HAR + живой сессии редактора (пост `967428`, июнь 2026).
> Документ описывает **авторский слой** Хабра: загрузку, сохранение и формат статей.
> Чтение лент/статей/комментариев описано отдельно (см. `src/client.py`).
>
> Все токены/куки в примерах **замаскированы** (`<...>`).

---

## 1. Две поверхности API

У Хабра один хост (`https://habr.com`) и один префикс (`/kek/v2/`), но логически — два разных API:

| Поверхность | Назначение | Авторизация |
|---|---|---|
| `articles/…`, `articles/<id>/comments/…` | чтение/лента/поиск/комменты/голоса | чтение — аноним; запись — сессия |
| **`publication/…`, `refs/…`** | **редактор статей: черновик, автосейв, публикация** | **только залогиненная сессия** |

Авторский слой (`publication/…`) **не доступен анонимно** и не отдаёт осмысленных ошибок без сессии — поэтому его и не получалось нащупать probe-запросами.

---

## 2. Авторизация для записи

Запросы редактора уходят с полным браузерным набором cookie + несколькими заголовками.

### Заголовки
```
csrf-token: <36-символьный токен>      # обязателен для write; из <meta name="csrf-token">
habr-user-uuid: <uuid>                  # дублирует cookie habr_uuid
x-app-version: 2.329.0                  # версия фронта; авто-определяется из ответного заголовка server-habr-version (значение — пример); шлётся
accept: application/json, text/plain, */*  # как у реального браузера
content-type: application/json
origin: https://habr.com
referer: https://habr.com/en/article/edit/<id>/
```
> csrf-токен — это **36-символьный токен** из `<meta name="csrf-token">` страницы редактора.

### Cookie (значимые для сессии)
```
connect_sid=s%3A<...>      # ОСНОВНАЯ сессия (Express connect.sid; имя с подчёркиванием!)
hsec_id=<hex>              # security-токен сессии
habrsession_id=<...>      # серверная сессия
habr_uuid=<...>           # = заголовок habr-user-uuid
habr_web_user_id=<id>     # числовой id пользователя
hl=en; fl=en%2Cru         # языки интерфейса/контента
```
Остальные куки (`_ga*`, `qrator_msid2`, `PHPSESSID`, `theme`, …) — аналитика/CDN, для API не нужны.

> ⚠️ **Важно для MCP (подтверждено на живой сессии):** прошлый прототип предполагал
> `connect.sid` (точка). В реальном браузере кука называется **`connect_sid`**
> (подчёркивание), и одной её мало — сессию держит связка
> `connect_sid` + `hsec_id` + `habrsession_id` + `habr_uuid` (+ `habr_web_user_id`).
> Надёжнее хранить **весь Cookie-заголовок целиком**, а не отдельные поля.

---

## 3. Жизненный цикл редактирования статьи

**Создание** нового черновика (кнопка «Написать»):
```
POST /kek/v2/publication/save            (БЕЗ id!) → создаёт черновик
                                          ответ: {"post":"<id>","ok":true}  (новый id в "post")
```

**Открытие/правка** существующего `https://habr.com/ru/article/edit/<id>/`:
```
GET  /kek/v2/publication/post-data/<id>                  → загрузить форму поста (title, text, hubs, …)
GET  /kek/v2/publication/wysiwyg-rules                   → какие элементы разрешены в каждой зоне
GET  /kek/v2/refs/flows/wysiwyg?publicationId=<id>       → список потоков (flows)
GET  /kek/v2/publication/suggest-hubs?publicationType=topic&postType=simple&post=<id>&postContext=topic
                                                          → каталог хабов (alias ↔ числовой id)
GET  /kek/v2/publication/suggest-tags?q=<text>           → автокомплит тегов
POST /kek/v2/publication/save/<id>                       → автосейв: тело = вся форма поста
```

### Эндпоинты

| Метод | Путь | Назначение | Тело / ответ |
|---|---|---|---|
| `POST` | **`publication/save`** (без id) | **создать черновик** | тело: форма (см. §6); ответ: `{"post":"<id>","ok":true}` |
| `GET` | `publication/post-data/<id>` | прочитать черновик/пост | ответ: `{postForm, author}` (~115 КБ) |
| `POST` | `publication/save/<id>` | сохранить (автосейв) | тело: `postForm` (JSON); **ответ: пустой `200`** |
| `GET` | `publication/wysiwyg-rules` | правила форматирования по зонам | `{wysiwygRuleRefs:{zone:{elements:[…]}}}` |
| `GET` | `refs/flows/wysiwyg?publicationId=<id>` | список потоков | `{flows:[{id,title,alias}]}` |
| `GET` | `publication/suggest-hubs?…` | каталог хабов | `{collective[],offtopic[],corporative[],byPost[]}` |
| `GET` | `publication/suggest-tags?q=<text>` | автокомплит тегов | `{…}` |
| `GET` | `publication/suggest-banners?…` / `suggest-multiwidgets?…` | спецблоки | **`403`** (только корп-блоги) |
| `POST` | **`publication/upload`** | загрузить картинку/обложку | `multipart/form-data` (файл) → JSON с URL на `habrastorage.org` (см. §6.3) |
| `GET` | **`articles/drafts?user=<login>&draftType=posts&page=&perPage=`** | **список черновиков автора** (требует алиас автора в `user` и `draftType`; путь БЕЗ завершающего слэша) ✅ подтверждён вживую | ответ: `{pagesCount, publicationIds, publicationRefs}` |
| `GET` | **`me`** | текущий пользователь (для алиаса в `user`) ✅ подтверждён вживую | `{id, alias, fullname, …}` |
| `DELETE` | **`articles/drafts/<id>/posts`** | удалить черновик | тело `{}` → `{"ok":true}` |

> **Подтверждено на живой сессии:** `POST publication/save` без id создал черновик
> (ответ `{"post":"1052540","ok":true}` — новый id в ключе **`post`**). Автосейв
> `POST save/<id>` возвращает **пустое тело `200`** — уже учтено в `_parse` (пустой 2xx =
> успех). Загрузка картинки (`publication/upload`, поле `file`) и удаление
> (`DELETE articles/drafts/<id>/posts` → `{"ok":true}`) тоже подтверждены вживую.

### ⚠️ Оставшийся пробел
- **Публикация** черновика (`drafted` → `published`/на модерацию). **Гипотеза:** тот же
  `save/<id>` со сменой `status` (`"drafted"` → `"unpublished"`/`"published"`), либо отдельный
  endpoint — **не подтверждено**, нужен реальный запрос кнопки «Опубликовать».
  (Создание, чтение, правка, автосейв, загрузка картинок и удаление — уже подтверждены.)

---

## 4. Структура `postForm` (тело `save` ≈ ответ `post-data`)

`save` отправляет ровно ту же форму, что отдаёт `post-data.postForm`. Ключевые поля:

| Поле | Тип | Смысл / пример |
|---|---|---|
| `id` | string | id поста/черновика — `"967428"` |
| `lang` | string | язык контента — `"ru"` |
| `type` | string | тип публикации — `"simple"` (обычная) / `"mega"` (мегапост) |
| `status` | string | `"draft"` / `"published"` |
| `publishedAt` | string\|null | ISO-дата публикации |
| `plannedDateTime`, `isPlanned` | string\|null, bool | отложенная публикация |
| `title` | string | заголовок |
| **`text`** | object | **тело статьи** (см. §5) |
| **`preview`** | object | **анонс «до ката»** (тот же формат, что `text`) |
| `hubs` | int[] | **числовые id хабов** — `[161, 21900, 21924]` (резолв через `suggest-hubs`) |
| `tags` | string[] | теги — `["хабр", "блаблабла", …]` |
| `flow` | string | id потока — `"22"` (= `analytics`, см. `refs/flows`) |
| `format` | string\|null | формат поста — `"analytics"` (Аналитика), `"opinion"`, `"tutorial"`, … |
| `complexity` | string\|null | сложность — `"low"` / `"medium"` / `"high"` / `null` |
| `feedCover` | object\|null | обложка ленты — `{url, fit:"cover", positionX, positionY}` |
| `leadButtonText` | string | текст кнопки ката — `"Читать далее"` |
| `isTranslation` | bool | перевод? |
| `translationSource`, `originalAuthor` | string\|null | источник/автор оригинала (для переводов) |
| `isModerated`, `isLocked`, `isCorrectorConfirm`, `isCompanyExperience` | bool | флаги статуса/модерации |
| `draftReason` | string | причина возврата в черновики (от модератора) |
| `polls` | array | опросы |
| `banner`, `multiwidget` | object\|null | спецблоки |
| `idempotenceKey` | string | **только при создании** — nanoid, защита от дублей |

> **Типы при записи отличаются от чтения!**
> - `hubs` при записи — **массив строк** `["19791","4992"]` (в `post-data` приходят как int).
> - `text.editorVersion` при записи — **число `2`** (в `post-data` строка `"2"`).
> - `status` при создании — **`"drafted"`** (в `post-data` опубликованного поста — `"published"`).
> - `format` по умолчанию — **`"common"`** (Обычный); другие: `analytics`, `opinion`, `tutorial`, …

### 4.1 Минимальное реальное тело создания черновика

`POST /kek/v2/publication/save` (без id), `content-type: application/json`:

```json
{
  "lang": "ru",
  "type": "simple",
  "title": "проверка",
  "feedCover": null,
  "hubs": ["19791", "4992"],
  "tags": ["проверка внимательности"],
  "text": {
    "source": "{\"type\":\"doc\",\"content\":[{\"type\":\"heading\",\"attrs\":{\"level\":1,\"class\":null},\"content\":[{\"type\":\"text\",\"text\":\"проверкапроверкапроверка\"}]},{\"type\":\"paragraph\",\"attrs\":{\"simple\":false,\"persona\":false}}]}",
    "editorVersion": 2,
    "isMarkdown": false
  },
  "preview": {
    "source": "{\"type\":\"doc\",\"content\":[{\"type\":\"paragraph\",\"attrs\":{\"simple\":false,\"persona\":false},\"content\":[{\"type\":\"text\",\"text\":\"…анонс…\"}]}]}",
    "editorVersion": 2,
    "isMarkdown": false
  },
  "leadButtonText": "Читать далее",
  "isTranslation": false,
  "format": "common",
  "isPlanned": false,
  "plannedDateTime": "2026-06-15T15:13:20.900Z",
  "translationSource": null,
  "originalAuthor": null,
  "isCompanyExperience": false,
  "flow": "2",
  "status": "drafted",
  "banner": null,
  "multiwidget": null,
  "idempotenceKey": "0hrun4FXrlIZS-fN4LPJ2"
}
```

> **Валидация формы (подтверждено телом `422` на живой сессии):**
> - `hubs` — минимум 1 элемент («Этот массив должен содержать 1 элемент или больше»);
> - `tags` — минимум 1 элемент (то же сообщение);
> - `flow` — обязателен и непустой («Необходимо заполнить поле»);
> - `preview` (анонс «до ката») — отрендеренный текст должен быть **100–3000 символов**
>   («Аннотация не может быть короче 100 символов и длиннее 3000 символов»).
>
> Прежнее наблюдение «422 = пустой preview» теперь обобщено до правила длины 100–3000.
> Поэтому `create_draft` валидирует hubs/tags/flow/длину анонса локально и отдаёт понятную
> ошибку **до** запроса.

---

## 5. Формат тела статьи (`text` / `preview`) — `editorVersion 2`

Поле `text` (и `preview`) — это объект из трёх частей:

```json
{
  "source": "{\"type\":\"doc\",\"content\":[ … ]}",   // ProseMirror-документ, СЕРИАЛИЗОВАННЫЙ В СТРОКУ
  "editorVersion": "2",
  "isMarkdown": false
}
```

- `source` — **JSON-строка** (не объект!) с ProseMirror-деревом `{"type":"doc","content":[…]}`.
- `editorVersion: "2"` — текущий редактор Хабра.
- `isMarkdown: false` — контент в виде дерева, не Markdown.

### 5.1 Справочник узлов (node) — JSON исходника → HTML-рендер

Все формы ниже **подтверждены** на реальных статьях (`556124`, `868790`, `594895`, `689116`)
сопоставлением `post-data.text.source` ↔ `articles/<id>/.textHtml`.

| Узел | JSON в `source` | Рендер в HTML |
|---|---|---|
| **абзац** | `{"type":"paragraph","attrs":{"simple":false,"persona":false},"content":[…]}` ⚠️ ключа `align` **нет**, когда выравнивание null | `<p>…</p>` |
| **заголовок** | `{"type":"heading","attrs":{"level":1,"class":null},"content":[text]}` | **`level:1` → `<h2>`**, `2`→`<h3>`, `3`→`<h4>` (h1 занят заголовком статьи) |
| **перенос строки** | `{"type":"hard_break"}` | `<br>` |
| **список** | `{"type":"unordered_list"\|"ordered_list","attrs":{"type":"outer"},"content":[listitem…]}` ⚠️ элемент — `listitem` (одно слово), вложенный список — `attrs.type:"inner"` | `<ul>/<ol><li>…</li></ul>` |
| **цитата** | `{"type":"blockquote","content":[paragraph…]}` | `<blockquote>…</blockquote>` |
| **код-блок** | `{"type":"code_block","attrs":{"lang":"bash","code":"…текст кода…"}}` ⚠️ код в `attrs.code`, **не** в content | `<pre><code class="bash">…</code></pre>` |
| **спойлер** | `{"type":"spoiler","attrs":{"title":"Дисклеймер"},"content":[paragraph…]}` | `<details class="spoiler"><summary>Дисклеймер</summary>…</details>` |
| **картинка** | см. §5.3 | `<img …>` (в `<figure>` с подписью) |
| **разделитель** | `{"type":"hr","attrs":{"inserted":true}}` | `<hr>` |
| **таблица** | см. §5.4 | `<table>…</table>` |
| **формула (блок)** | `{"type":"formula","attrs":{"source":"a^2"}}` ⚠️ LaTeX в `attrs.source` | блочная формула |
| **формула (инлайн)** | `{"type":"inline_formula","attrs":{"source":"x_i"}}` ⚠️ LaTeX в `attrs.source` | инлайн-формула |
| **встраивание** | `{"type":"embed","attrs":{"src":"https://youtu.be/…","inserted":false}}` см. §5.5 | iframe/oEmbed |

> Заголовки начинаются с **`level:1`** (= `<h2>`). Это важно: в редакторе «Заголовок 1»
> рендерится как `h2`, потому что `h1` — это название статьи.
> Код-блок хранит текст в `attrs.code` (НЕ в дочерних узлах) и язык в `attrs.lang`.
>
> **Списки (подтверждено на живой API, 200):** канонический вид редактора —
> `unordered_list`/`ordered_list` с `attrs:{"type":"outer"}` (верхний уровень) или
> `"inner"` (список прямо внутри элемента списка) → элемент **`listitem`** (одно слово,
> без `attrs`) → `paragraph`. Имя `list_item` (snake_case) Хабр **отклоняет** с `422`.

### 5.2 Справочник марок (mark) — инлайн-форматирование текста

Марки вешаются на узел `text` через массив `marks`:
```jsonc
{"type":"text","text":"важно","marks":[{"type":"bold"}]}
{"type":"text","text":"ссылка","marks":[{"type":"link","attrs":{"href":"https://…"}}]}
```

| Марка | JSON | Рендер |
|---|---|---|
| `bold` / `italic` / `strike` / `underline` | `{"type":"bold"}` | `<b>` / `<i>` / `<s>` / `<u>` |
| `sup` / `sub` | `{"type":"sup"}` | `<sup>` / `<sub>` |
| `code` | `{"type":"code"}` | `<code>` (инлайн) |
| `link` | `{"type":"link","attrs":{"href":"https://…"}}` | `<a href="…">` |
| `abbr` | `{"type":"abbr","attrs":{"title":"расшифровка"}}` | `<abbr title="…">` |

### 5.3 Картинки в теле (узел `image`)

```jsonc
{
  "type": "image",
  "attrs": {
    "src": "https://habrastorage.org/getpro/habr/upload_files/6e5/75a/237/6e57….jpg",  // URL из upload (§6.x)
    "alt": null, "title": null,
    "width": 1220, "height": 2712,     // реальные пиксели картинки
    "fullWidth": true,                  // на всю ширину колонки
    "border": false, "float": false,
    "customClass": "", "gallery": false, "inserted": false
  },
  "content": [{"type": "image_caption"}]   // подпись (пустая, либо текст внутри)
}
```
Файл сначала загружается на `habrastorage.org` (см. §6.x), затем его URL подставляется
в `attrs.src`. Подпись — дочерний узел `image_caption`.

### 5.4 Таблицы (узел `table_wrapper`)

Снято из ProseMirror-состояния нативного редактора и проверено round-trip'ом на живой
API (`200`). Таблица оборачивается в `table_wrapper` → `table` → `table_row` →
`table_cell` → **`table_paragraph`** (в ячейке — `table_paragraph`, НЕ `paragraph`!).

```jsonc
{"type":"table_wrapper","content":[
  {"type":"table","content":[
    {"type":"table_row","content":[
      {"type":"table_cell","attrs":{"colspan":1,"rowspan":1,"colwidth":null},"content":[
        {"type":"table_paragraph","attrs":{"align":null},"content":[{"type":"text","text":"A1"}]}
      ]}
    ]}
  ]}
]}
```

- `table_cell.attrs`: `colspan`/`rowspan` (целые, по умолчанию `1`), `colwidth` (как есть, по
  умолчанию `null`). Отдельного заголовочного узла нет — Docmost `tableHeader` тоже
  маппится в `table_cell` (стиль заголовка теряется).
- `table_paragraph.attrs.align` = `null`, если у источника нет реального выравнивания;
  ключ `content` опускается у пустого абзаца. В ячейке всегда минимум один `table_paragraph`.

### 5.5 Встраивания (узел `embed`)

Снято из нативного редактора и проверено round-trip'ом (`200`). Минимальная форма:

```jsonc
{"type":"embed","attrs":{"src":"https://youtu.be/…","inserted":false}}
```

Хабр сам резолвит oEmbed на сервере (`GET https://embedd.srv.habr.com/geturl?v=v1&q=<url>`),
поэтому клиенту НЕ нужно ничего скачивать — достаточно положить URL в `attrs.src`.
Docmost `embed` и `youtube` (URL в `attrs.src`) маппятся сюда.

### Минимальный валидный документ
```jsonc
{"type":"doc","content":[
  {"type":"heading","attrs":{"level":1,"class":null},"content":[{"type":"text","text":"Заголовок"}]},
  {"type":"paragraph","attrs":{"simple":false,"persona":false},"content":[{"type":"text","text":"Текст абзаца."}]}
]}
```
Пустой абзац (конец документа) — `{"type":"paragraph","attrs":{"simple":false,"persona":false}}` (без `content`).

### Полный список разрешённых элементов (из `wysiwyg-rules`)

Зоны статьи: **`postLead`** (анонс) и **`postFull`** (тело). Для тела (`postFull`) разрешено:

```
hidden, Anchor, link, table, title, ordered_list, unordered_list,
inline_image, inline_formula, formula, spoiler, mention, code, blockquote,
embed, hr, heading, code_block, image, bold, italic, strike, sup, sub,
underline, abbr, persona
```

Анонс (`postLead`) — урезанный набор (без блочных: только inline-форматирование, `inline_image`,
`inline_formula`, `code`, `mention`). Есть и другие зоны (`postComment`, `thread`, `docs`, …) —
например **комментарии** (`postComment`) тоже принимают `editorVersion 2`.

---

## 6. Справочники

### Потоки (flows) — `refs/flows/wysiwyg`
Поле `flow` в форме = **id** одного потока (строка):

| id | alias | id | alias |
|---|---|---|---|
| 2 | backend | 24 | design |
| 4 | frontend | 26 | management |
| 6 | mobile_development | 28 | top_management |
| 8 | admin | 30 | marketing |
| 10 | information_security | 34 | sales |
| 12 | ai_and_ml | 36 | human_resources |
| 14 | industrial_engineering | 38 | back_office |
| 16 | gamedev | 40 | zero-code_development |
| 18 | quality_assurance | 42 | hardware_and_gadgets |
| 20 | support | 44 | diy |
| 22 | analytics | 46 | healthcare |
| — | — | 48 | popsci |
| — | — | 50 | other |

### Хабы — `suggest-hubs`
Поле `hubs` в форме = массив **числовых id**. Каталог отдаёт `suggest-hubs`, сгруппированный:
```jsonc
{
  "collective":  [{"id":"23108","alias":"smol","title":"$mol *", "isCorporative":false}, … 435],
  "offtopic":    [{"id":"19259","alias":"closet", …}, …],
  "corporative": [{"id":"19791","alias":"ruvds", "isCorporative":true, "tariffId":"giant_plus"}, …],
  "byPost":      [{"id":"161","alias":"habr","title":"Habr"}, …]   // хабы, уже привязанные к посту
}
```
Чтобы задать хаб по человекочитаемому `alias`, нужно сматчить его в этом каталоге → взять `id`.
В нашем примере `hubs:[161,21900,21924]` ↔ `habr` + ещё два.

### 6.3 Загрузка изображений — `POST publication/upload`

Картинки (и обложка ленты) грузятся **до** сохранения формы, отдельным запросом:

```
POST /kek/v2/publication/upload
Content-Type: multipart/form-data; boundary=----WebKitFormBoundary…
Accept: application/json
(тело: файл одним полем формы с именем "file")          # ⚠️ имя поля — file (подтверждено)
→ 200, JSON {"url":"https://habrastorage.org/.../<hash>.<ext>"}  # ⚠️ URL в ключе "url" (подтверждено)
```

Дальше полученный URL подставляется:
- в **тело статьи** — как `image.attrs.src` (узел `image`, см. §5.3);
- в **обложку ленты** — как `feedCover.url`:
  ```json
  "feedCover": {"url": "https://habrastorage.org/getpro/habr/upload_files/eb5/…/….jpg",
                "fit": "cover", "positionX": 0, "positionY": 0}
  ```

> **Подтверждено на живой API:** имя поля multipart — **`file`**, ответ — JSON
> `{"url":"https://habrastorage.org/.../<hash>.<ext>"}`.

---

## 7. Итоговая модель «как опубликовать статью» (реконструкция)

1. ✅ `POST publication/save` (без id) с формой (`status:"drafted"` + `idempotenceKey`) → новый `<id>`.
2. ✅ `GET publication/post-data/<id>` → прочитать текущую `postForm`.
3. ✅ Сформировать `text.source` как ProseMirror-дерево `editorVersion 2` (заголовки/абзацы/марки),
   заполнить `title`, `hubs` (id-строки), `tags`, `flow`, `format`, `complexity`, `preview`.
4. ✅ Картинки/обложку — `POST publication/upload` → URL в `image.attrs.src` / `feedCover.url`.
5. ✅ `POST publication/save/<id>` с формой → автосейв (ответ пустой `200`).
6. ✅ Удалить черновик — `DELETE articles/drafts/<id>/posts` → `{"ok":true}`.
7. **(пробел)** перевести `drafted` → `published`/на модерацию — запрос не пойман.

Реализуемы прямо сейчас: **создание, чтение, правка текста, картинки, автосейв, удаление**.
Не хватает только **публикации** (шаг 7) — и точных ключей ответа `upload`.

---

## 8. Открытые вопросы (нужны ещё запросы из браузера)

Осталось снять (Copy as cURL, **с телом**) реальные запросы:
- **«Опубликовать» / «Отправить на модерацию»** — смена `status` черновика. Проверить гипотезу,
  что это тот же `save/<id>` с другим `status`. ← **единственный оставшийся пробел**

Закрыто на живой сессии (раньше были открытыми вопросами):
- ✅ `POST publication/upload` — поле формы **`file`**, ответ `{"url":"https://habrastorage.org/…"}`.
- ✅ Тело `422`-ответа создания — правила валидации: `hubs`≥1, `tags`≥1, `flow` обязателен,
  `preview` (анонс) 100–3000 символов (см. §4.1).
- ✅ Канонические узлы тела: список `outer`/`inner` → `listitem` → `paragraph`; `code_block`
  с кодом в `attrs.code`; `paragraph` без `align`, когда выравнивание null.

---

## 9. Заметки для будущего MCP-API (черновик, не финал)

Возможные инструменты авторского слоя (детали — отдельно, по обсуждению):
- `create_draft(title, body, hubs, …)` — ✅ `POST save` без id (`status:"drafted"` + idempotenceKey).
- `get_draft(id)` — ✅ чтение `post-data` в человекочитаемом виде.
- `list_drafts()` — ✅ реализован через `GET articles/drafts` (алиас автора берётся из `me` → `alias`).
- `update_draft(id, …)` — ✅ правка полей + автосейв (`save/<id>`). Тело текста — из Markdown
  конвертить в `editorVersion 2` ProseMirror (нетривиально: нужен сериализатор).
- `upload_image(path)` — ✅ `POST publication/upload` → URL (уточнить имя поля/ключи ответа).
- `delete_draft(id)` — ✅ `DELETE articles/drafts/<id>/posts`.
- `publish_draft(id)` — ⏳ **заблокирован** до получения запроса публикации.
- `resolve_hubs(aliases)` / `list_flows()` — справочники из `suggest-hubs` / `refs/flows`.

Главная сложность авторского слоя — **не HTTP, а формат контента**: тело пишется деревом
ProseMirror `editorVersion 2`, поэтому понадобится конвертер Markdown/HTML → это дерево
(и обратно для чтения).
