---
name: Telegram Rich Messages
description: Спокойная локальная мастерская постов с нейтральной палитрой и читаемой рабочей плотностью.
colors:
  ink: "#17181c"
  muted: "#626976"
  paper: "#ffffff"
  canvas: "#f4f7fb"
  line: "#dfe4eb"
  line-strong: "#cbd2dd"
  focus: "#2f80ed"
  link: "#245aa1"
  success: "#176944"
  warning: "#855315"
  warning-bg: "#fff7e8"
  error: "#a73535"
  error-bg: "#fff0ef"
typography:
  headline:
    fontFamily: 'Onest, "Segoe UI", sans-serif'
    fontSize: "28px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  headline-mobile:
    fontFamily: 'Onest, "Segoe UI", sans-serif'
    fontSize: "25px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  title:
    fontFamily: 'Onest, "Segoe UI", sans-serif'
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.018em"
  body:
    fontFamily: 'Onest, "Segoe UI", sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: 'Onest, "Segoe UI", sans-serif'
    fontSize: "13px"
    fontWeight: 550
    lineHeight: 1.6
  mono:
    fontFamily: '"JetBrains Mono", monospace'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  control: "4px"
spacing:
  compact: "8px"
  control: "12px"
  section: "20px"
  group: "24px"
  column: "32px"
components:
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.canvas}"
  button-text:
    textColor: "{colors.link}"
    padding: "4px 0"
  input:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  navigation:
    textColor: "{colors.muted}"
    padding: "0 0 14px"
  navigation-active:
    textColor: "{colors.ink}"
  status:
    textColor: "{colors.muted}"
    size: "12px"
  preview:
    backgroundColor: "{colors.canvas}"
    rounded: "{rounded.control}"
    padding: "30px 32px"
  calendar-day-selected:
    backgroundColor: "{colors.link}"
    textColor: "{colors.paper}"
    rounded: "{rounded.control}"
    height: "32px"
---

# Дизайн-система дашборда

## Overview

**Creative North Star: "Локальная мастерская"**

Спокойная рабочая поверхность в режиме Operate: текст и материал ведут взгляд, цвет отмечает состояние, выбор и focus. Визуальная основа по прямому указанию пользователя — `/Users/evgenyzach/Project/voice-typing-manual/style.css`. Сохранены локальные шрифты, нейтральная палитра, геометрия контролов и ритм; источник не менялся.

Документ описывает фактический `web/style.css` и компоненты `web/shell.html`/`web/app.js`. Сценарий этой поверхности зафиксирован отдельно в `web/SURFACE-BRIEF.md`. Маркетинговая композиция исходного проекта сюда не переносится.

**Key Characteristics:**

- Светлая бумага и прохладная подложка.
- Onest для чтения, JetBrains Mono для технических идентификаторов.
- Тонкие границы, компактные контролы и явный клавиатурный focus.
- Цветовое состояние сопровождается текстом.

## Colors

Нейтральная основа и дозированные функциональные акценты; нормативные значения находятся во frontmatter.

### Primary

- **Функциональный синий** (`focus`): текущий этап и caret. Клавиатурный focus контролов — тонкий чернильный контур.
- **Спокойный синий ссылки** (`link`): ссылки, текстовые действия и выбранный день календаря.

### Neutral

- **Чернильный текст** (`ink`) и **прохладный вторичный текст** (`muted`): основное чтение и метаданные.
- **Белая бумага** (`paper`) и **прохладная подложка** (`canvas`): страница и локальные рабочие области.
- **Тихая граница** (`line`) и **контрольная граница** (`line-strong`): разделение строк и контуры полей.

Семантические `success`, `warning`, `error` обозначают результат. `warning-bg` и `error-bg` используются в поясняющих уведомлениях. Статусы опубликован/ошибка/отправляется/неизвестно различаются также подписью.

**The Status With Words Rule.** Цвет состояния всегда сопровождается читаемым текстом.

## Typography

Основной и заголовочный шрифт — локальный Onest; технические идентификаторы и code — локальный JetBrains Mono. Оба подключены через `@font-face` с `font-display: swap`. Лицензии: `web/fonts/Onest-OFL.txt`, `web/fonts/JetBrainsMono-OFL.txt`; файлы шрифтов перенесены без изменения.

Иерархия страницы использует `headline`, `title`, `body`, `label` из frontmatter. При ширине до 780 px заголовок переходит на `headline-mobile`. Внутри превью собственная документная шкала: h1 (25 px), h2 (22 px), h3 (18 px); до 500 px — h1 (22 px), h2 (20 px), текст (14 px). Мелкие метаданные и статусы (11–14 px) подчинены основному тексту. Абзац превью ограничен 72ch.

**The Reading Hierarchy Rule.** Заголовок страницы, заголовок раздела и основной текст сохраняют разный размер и вес.

## Layout

Контейнер ограничен шириной (1240 px); desktop отступает от краёв на (32 px). Основной пост: гибкое превью и колонка метаданных (290 px) с промежутком (54 px). История: гибкая таблица и календарь (280 px), промежуток (40 px). Шаги compact/control/section/group/column в frontmatter отражают повторяемый ритм, а не обязательную универсальную сетку.

При 1100 px колонки и промежутки уплотняются, фильтры становятся двухколоночными. При 780 px пост складывается в одну колонку, история ставит календарь перед таблицей; поля контейнера (20 px). До 500 px поля (16 px), календарь становится единым блоком, превью уплотняется. Минимальная ширина body (320 px). Широкие таблицы прокручиваются внутри своей области.

**The Contained Overflow Rule.** Широкий материал прокручивается внутри превью или таблицы, не расширяя всю страницу.

## Elevation & Depth

В текущем дашборде нет box-shadow. Глубину дают прохладная подложка, тонкие разделители и контуры. Это описание этой реализации: тени и градиент исходного voice-typing-manual не переносились как обязательные элементы нового интерфейса.

**The Quiet Surface Rule.** В этой рабочей поверхности материал отделяется тоном и границей.

## Shapes

Контролы, превью, уведомления и календарь используют небольшой радиус `control`. Кружки статуса и календарные точки остаются круглыми. Подчёркивание вкладки прямое; клавиатурный focus — внешний чернильный контур (2 px) с отступом (2 px). При обычном клике мышью внешний контур не рисуется.

## Components

### Buttons

Спокойное действие: контурная кнопка для пагинации и текстовая для сброса. Контурная кнопка использует `button-secondary`, высоту не меньше (40 px), границу `line-strong`, hover на `canvas`; active слегка затемняет подложку. Disabled снижает opacity (.45). Текстовая кнопка на hover подчёркивается. Переход background/border (160 ms) использует `cubic-bezier(.16,1,.3,1)`; reduced motion отключает переходы.

### Inputs / Fields

Ясный редактируемый контур: `input` из frontmatter, граница `line-strong`, минимальная высота (44 px). При hover граница усиливается; focus использует общий внешний контур. Подпись находится над полем. Ошибка диапазона дат получает текстовое уведомление и `aria-invalid`.

«Выбранный пост» и «Статус» используют общий combobox с локальным popup: Onest 15 px, внутренние отступы 10×14 px, SVG-стрелка 18 px с отступом 14 px от правой границы. Меню отделено промежутком 6 px и тонкой границей; выбранный пункт выделяется весом, активный — подложкой canvas. Поддержаны стрелки, Home/End, Enter/Space, Escape, Tab и поиск по началу названия. Native select сохраняет значения/события и остаётся резервом до запуска JS.

### Navigation

Текстовые вкладки на общей нижней линии с промежутком (28 px). Активная вкладка отмечена `aria-current`, чернильным текстом и нижней линией (2 px); hover усиливает текст. Клавиатурный focus остаётся видимым. Композиция вкладок сохраняется на mobile.

### Cards / Containers

Превью и календарь — плоские области на `canvas` с небольшим радиусом. Превью использует `preview` из frontmatter, на узком экране padding (24 px), до 500 px (20 px). Календарь имеет padding (20 px), на mobile (18 px). Универсальной библиотеки карточек в проекте нет.

### Status

Короткая подпись с точкой (6 px), текст (12 px). Draft нейтрален; published использует success, error — error, sending/unknown — warning. Это текстовый индикатор без клика, не action-chip.

### Calendar

Семь равных колонок с промежутком (4 px), дни высотой (32 px). Точка отмечает наличие постов; выбранный день использует `calendar-day-selected`, текущий — тонкий внутренний контур. Дни без постов disabled. Название месяца и стрелки находятся над сеткой.

## Do's and Don'ts

### Do:

- **Do** Сохранять локальные Onest и JetBrains Mono и их лицензии.
- **Do** Сопровождать цвет состояния читаемой подписью.
- **Do** Сохранять внешний клавиатурный focus и reduced motion.
- **Do** Проверять ширину страницы и внутреннюю прокрутку таблиц на mobile.

### Don't:

- **Don't** Подменять рабочую иерархию одинаковым размером текста.
- **Don't** Считать календарь расписанием: он фильтрует историю.
- **Don't** Переносить маркетинговые сценарии voice-typing-manual в дашборд.
