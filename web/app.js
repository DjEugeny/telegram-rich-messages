"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {draft: "Черновик", sending: "Отправляется", published: "Опубликован", error: "Ошибка", unknown: "Результат неизвестен"};
  const scheduleLabels = {reviewed: "Проверен для согласования", sending: "Предложение отправляется", proposed: "Предложение принято; расписание не подтверждено", approving: "Ожидается одобрение расписания", scheduled: "Запланирован", unknown: "Исход расписания неизвестен", error: "Ошибка расписания", cancelled: "Отмена подтверждена человеком", published: "Публикация подтверждена человеком"};
  const storageKey = "telegram-rich-messages:view:v1";
  const selectControls = new Map();

  // Keep native values/events as the data contract; render the popup in Onest.
  function enhanceSelect(select, name) {
    const wrapper = document.createElement("div");
    wrapper.className = "select-control";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.id = select.id + "-trigger";
    trigger.className = "select-trigger";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-label", name);
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const value = document.createElement("span");
    value.className = "select-value";
    const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    arrow.setAttribute("viewBox", "0 0 20 20");
    arrow.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(arrow.namespaceURI, "path");
    path.setAttribute("d", "m5 7.5 5 5 5-5");
    arrow.append(path);
    trigger.append(value, arrow);
    const menu = document.createElement("div");
    menu.id = select.id + "-listbox";
    menu.className = "select-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", name);
    menu.hidden = true;
    trigger.setAttribute("aria-controls", menu.id);
    let active = 0;
    let query = "";
    let typedAt = 0;
    const options = () => [...select.options];
    function refresh() {
      value.textContent = select.selectedOptions[0]?.textContent || "Нет постов";
      trigger.disabled = select.disabled;
    }
    function close() {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      trigger.removeAttribute("aria-activedescendant");
    }
    function highlight(index) {
      active = Math.max(0, Math.min(index, select.options.length - 1));
      [...menu.children].forEach((item, i) => item.classList.toggle("is-active", i === active));
      const item = menu.children[active];
      if (item) {
        trigger.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({block: "nearest"});
      }
    }
    function choose(index) {
      if (!select.options[index] || select.options[index].disabled) return;
      select.selectedIndex = index;
      refresh();
      close();
      select.dispatchEvent(new Event("input", {bubbles: true}));
      select.dispatchEvent(new Event("change", {bubbles: true}));
      trigger.focus({preventScroll: true});
    }
    function open() {
      selectControls.forEach((control) => control.close());
      menu.replaceChildren(...options().map((option, index) => {
        const item = document.createElement("div");
        item.id = menu.id + "-" + index;
        item.className = "select-option";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(option.selected));
        item.setAttribute("aria-disabled", String(option.disabled));
        item.textContent = option.textContent;
        item.addEventListener("click", (event) => {event.preventDefault(); event.stopPropagation(); choose(index);});
        item.addEventListener("pointermove", () => highlight(index));
        return item;
      }));
      const rect = trigger.getBoundingClientRect();
      wrapper.classList.toggle("opens-up", rect.bottom + Math.min(320, innerHeight * .4) > innerHeight - 12 && rect.top > innerHeight - rect.bottom);
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      highlight(Math.max(0, select.selectedIndex));
    }
    trigger.addEventListener("click", () => menu.hidden ? open() : close());
    trigger.addEventListener("keydown", (event) => {
      const key = event.key;
      if (key === "Tab") {close(); return;}
      if (key === "Escape") {event.preventDefault(); close(); return;}
      if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(key)) {
        event.preventDefault();
        if (menu.hidden) {open(); if (key === "Home") highlight(0); if (key === "End") highlight(select.options.length - 1); return;}
        if (key === "Enter" || key === " ") choose(active);
        else highlight(key === "Home" ? 0 : key === "End" ? select.options.length - 1 : active + (key === "ArrowDown" ? 1 : -1));
      } else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        const now = Date.now();
        query = (now - typedAt < 700 ? query : "") + key.toLocaleLowerCase("ru");
        typedAt = now;
        if (menu.hidden) open();
        const index = options().findIndex((option) => option.textContent.toLocaleLowerCase("ru").startsWith(query));
        if (index >= 0) highlight(index);
      }
    });
    document.addEventListener("pointerdown", (event) => {if (!wrapper.contains(event.target)) close();});
    wrapper.addEventListener("focusout", (event) => {if (!wrapper.contains(event.relatedTarget)) close();});
    select.addEventListener("change", refresh);
    const fieldLabel = select.labels[0];
    select.before(wrapper);
    wrapper.append(trigger, menu);
    select.hidden = true;
    if (fieldLabel) fieldLabel.htmlFor = trigger.id;
    selectControls.set(select.id, {refresh, close});
    refresh();
  }
  let posts;
  try {
    posts = JSON.parse($("post-data").textContent);
    if (!Array.isArray(posts)) throw new Error("Invalid post collection");
  } catch {
    $("load-error").hidden = false;
    $("post-content").hidden = true;
    $("post-select").disabled = true;
    return;
  }

  function dateKey(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }
  function formatDate(value, withTime = false) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Не указано";
    return new Intl.DateTimeFormat("ru-RU", {day: "2-digit", month: "short", year: "numeric", ...(withTime ? {hour: "2-digit", minute: "2-digit"} : {})}).format(date);
  }
  const searches = new Map();
  const dayCounts = new Map();
  posts.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
  for (const post of posts) {
    const fragment = new DOMParser().parseFromString([post.html || "", ...(post.schedule || []).map((schedule) => schedule.html || "")].join(" "), "text/html");
    searches.set(post.id, `${post.title || ""} ${fragment.body.textContent || ""}`.toLocaleLowerCase("ru"));
    const day = dateKey(post.updated_at);
    if (day) dayCounts.set(day, (dayCounts.get(day) || 0) + 1);
  }

  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    if (!saved || typeof saved !== "object") saved = {};
  } catch {
    $("storage-notice").hidden = false;
  }
  const state = {postId: posts.some((post) => post.id === saved.postId) ? saved.postId : posts[0]?.id, tab: saved.tab === "history" ? "history" : "post", page: 1, day: ""};
  const initialMonth = posts[0] ? new Date(posts[0].updated_at) : new Date();
  let calendarMonth = new Date(initialMonth.getFullYear(), initialMonth.getMonth(), 1);

  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({postId: state.postId, tab: state.tab}));
    } catch {
      $("storage-notice").hidden = false;
    }
  }
  function setTab(tab, moveFocus = false) {
    if (tab !== "post") pauseMedia($("view-post"));
    state.tab = tab;
    for (const name of ["post", "history"]) {
      $("view-" + name).hidden = name !== tab;
      if (name === tab) $("tab-" + name).setAttribute("aria-current", "page");
      else $("tab-" + name).removeAttribute("aria-current");
    }
    if (moveFocus) $("tab-" + tab).focus();
    persist();
  }
  function statusNode(post) {
    const status = document.createElement("span");
    const known = Object.hasOwn(labels, post.status) ? post.status : "unknown";
    status.className = "status " + known;
    status.textContent = labels[known];
    return status;
  }
  function metadata(label, value, mono = false) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value === undefined || value === null || value === "" ? "Не указано" : String(value);
    if (mono) definition.className = "mono";
    row.append(term, definition);
    return row;
  }
  function pauseMedia(container) {
    container.querySelectorAll("video").forEach((video) => video.pause());
  }
  function prepareMedia(preview = $("preview")) {
    preview.querySelectorAll("video").forEach((video) => {
      const showError = () => {
        video.closest(".rich-media-item").querySelector(".media-error").hidden = false;
      };
      video.addEventListener("error", showError);
      if (video.error) showError();
    });
    preview.querySelectorAll('.rich-media-group[data-layout="slideshow"]').forEach((group) => {
      const slides = [...group.querySelectorAll(":scope > .rich-media-item")];
      if (!slides.length) return;
      group.setAttribute("role", "region");
      group.setAttribute("aria-roledescription", "слайд-шоу");
      group.setAttribute("aria-label", "Медиа поста");
      group.tabIndex = 0;
      slides.forEach((slide, index) => {
        slide.setAttribute("role", "group");
        slide.setAttribute("aria-roledescription", "слайд");
        slide.setAttribute("aria-label", `${index + 1} из ${slides.length}`);
      });
      const navigation = document.createElement("div");
      navigation.className = "media-navigation";
      const previous = document.createElement("button");
      const next = document.createElement("button");
      for (const button of [previous, next]) {
        button.type = "button";
        button.className = "button";
      }
      previous.textContent = "Назад";
      previous.setAttribute("aria-label", "Предыдущий слайд");
      next.textContent = "Далее";
      next.setAttribute("aria-label", "Следующий слайд");
      const counter = document.createElement("span");
      counter.className = "media-counter";
      counter.setAttribute("aria-live", "polite");
      counter.setAttribute("aria-atomic", "true");
      let current = 0;
      function show(index) {
        if (index < 0 || index >= slides.length) return;
        if (index !== current) pauseMedia(slides[current]);
        current = index;
        slides.forEach((slide, position) => {slide.hidden = position !== current;});
        previous.disabled = current === 0;
        next.disabled = current === slides.length - 1;
        counter.textContent = `${current + 1} из ${slides.length}`;
      }
      previous.addEventListener("click", () => show(current - 1));
      next.addEventListener("click", () => show(current + 1));
      group.addEventListener("keydown", (event) => {
        if (event.target.closest("video, a, input, select, textarea") || event.altKey || event.ctrlKey || event.metaKey) return;
        const target = {ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: slides.length - 1}[event.key];
        if (target === undefined) return;
        event.preventDefault();
        show(target);
      });
      navigation.append(previous, counter, next);
      group.append(navigation);
      show(0);
    });
  }
  function scheduleDate(schedule) {
    try {
      return new Intl.DateTimeFormat("ru-RU", {timeZone: schedule.timezone, day: "2-digit", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(schedule.send_at)) + ` · ${schedule.timezone}`;
    } catch {
      return `${schedule.send_at || "Дата не указана"} · ${schedule.timezone || "Зона не указана"}`;
    }
  }
  function scheduleStatus(schedule) {
    const span = document.createElement("span");
    const known = Object.hasOwn(scheduleLabels, schedule.status) ? schedule.status : "unknown";
    span.className = "status " + known;
    span.textContent = scheduleLabels[known];
    return span;
  }
  function refreshScheduleDeadlines() {
    const now = Date.now();
    $("schedule-list").querySelectorAll("[data-schedule-deadline]").forEach((notice) => {
      notice.hidden = !(Number(notice.dataset.scheduleDeadline) <= now);
    });
  }
  function renderSchedule(post) {
    pauseMedia($("schedule-list"));
    const schedules = post.schedule || [];
    $("schedule-section").hidden = schedules.length === 0;
    $("schedule-empty").hidden = schedules.length > 0;
    $("schedule-list").replaceChildren(...schedules.map((schedule) => {
      const section = document.createElement("section");
      section.className = "schedule-entry";
      const heading = document.createElement("h3");
      heading.append(scheduleStatus(schedule));
      const meta = document.createElement("dl");
      meta.append(metadata("Выход", scheduleDate(schedule)), metadata("Канал", schedule.channel_title || schedule.channel), metadata("Согласованная версия", schedule.version), metadata("Текущая версия", post.version), metadata("Источник", schedule.source), metadata("ID предложения", schedule.proposal_message_id), metadata("ID опубликованного сообщения", schedule.published_message_id));
      const notice = document.createElement("p");
      notice.className = "notice";
      notice.textContent = `${schedule.experimental ? "Экспериментальный адаптер. " : ""}${schedule.live_verified ? "" : "Live-проверка не выполнена. "}${schedule.status === "published" ? "Публикация отмечена по свидетельству человека; автоматической проверки нет." : schedule.status === "scheduled" ? "Расписание подтверждено; публикация ещё не подтверждена." : "Это состояние не подтверждает запланированную публикацию."}`;
      const overdue = document.createElement("p");
      overdue.className = "notice";
      overdue.hidden = true;
      if (["scheduled", "proposed", "approving", "sending", "unknown"].includes(schedule.status)) {
        overdue.dataset.scheduleDeadline = String(new Date(schedule.send_at).getTime());
      }
      overdue.textContent = "Время наступило; публикация не подтверждена. Сверь результат в Telegram через агента.";
      const detail = document.createElement("details");
      detail.className = "scheduled-content";
      const summary = document.createElement("summary");
      summary.textContent = `Посмотреть согласованную версию ${schedule.version ?? "неизвестно"}${Number(schedule.version) !== Number(post.version) ? " · отличается от текущей" : ""}`;
      const content = document.createElement("article");
      content.className = "preview";
      // Only core.compile_post HTML from the immutable saved version.
      if (schedule.content_error) content.textContent = schedule.content_error;
      else content.innerHTML = schedule.html || "";
      prepareMedia(content);
      detail.append(summary, content);
      detail.addEventListener("toggle", () => {if (!detail.open) pauseMedia(detail);});
      section.append(heading, notice, overdue, meta);
      for (const [label, value] of [["Ошибка", schedule.last_error], ["Свидетельство человека", schedule.evidence]]) {
        if (!value) continue;
        const message = document.createElement("p");
        message.className = "notice";
        message.textContent = `${label}: ${value}`;
        section.append(message);
      }
      section.append(detail);
      return section;
    }));
    refreshScheduleDeadlines();
  }
  function selectPost(id, openTab = false) {
    const post = posts.find((entry) => entry.id === id);
    if (!post) return;
    state.postId = id;
    $("post-select").value = id;
    selectControls.get("post-select")?.refresh();
    $("post-position").textContent = `${posts.findIndex((entry) => entry.id === id) + 1} из ${posts.length}`;
    // html comes exclusively from core.compile_post; never interpolate metadata here.
    pauseMedia($("preview"));
    $("preview").innerHTML = post.html || "";
    prepareMedia();
    renderSchedule(post);
    const status = statusNode(post);
    $("post-status").className = status.className;
    $("post-status").textContent = status.textContent;
    const destination = post.destination || {};
    const publication = post.publication || {};
    const bot = destination.bot_username ? `@${String(destination.bot_username).replace(/^@/, "")}` : destination.bot_id;
    $("post-meta").replaceChildren(
      metadata("Чат", destination.chat_title || destination.chat_id || "Не выбран"),
      metadata("Бот", bot || "Не выбран"),
      metadata("Тема", destination.message_thread_id ?? "Без темы"),
      metadata("Версия", post.version),
      metadata("Изменён", formatDate(post.updated_at, true)),
      metadata("ID сообщения", publication.message_id ?? "Ещё не опубликован", Boolean(publication.message_id))
    );
    const publishedVersion = publication.published_version ?? publication.version;
    $("publication-difference").hidden = !publication.message_id;
    if (publication.message_id) {
      $("publication-difference").textContent = publishedVersion == null
        ? "Версия отправленного поста не указана. Сверь сообщение в Telegram."
        : Number(publishedVersion) === Number(post.version)
          ? `Опубликована текущая версия ${post.version}.`
          : `В Telegram — версия ${publishedVersion}. Здесь — версия ${post.version} с изменениями.`;
    }
    $("visual-state").textContent = post.visual_verified ? "Вид в Telegram проверен" : "Вид в Telegram ещё не проверен";
    $("visual-state").classList.toggle("verified", Boolean(post.visual_verified));
    $("message-link").hidden = true;
    $("message-link").removeAttribute("href");
    try {
      const link = new URL(publication.url || publication.link || "");
      if (link.protocol === "https:" && ["t.me", "telegram.me"].includes(link.hostname) && !link.username && !link.password) {
        $("message-link").href = link.href;
        $("message-link").hidden = false;
      }
    } catch { /* No confirmed public message link. */ }
    const activeStage = post.status === "published" ? 4 : ["sending", "unknown", "error"].includes(post.status) ? 3 : 2;
    document.querySelectorAll(".stages li").forEach((step, index) => {
      step.classList.toggle("complete", index < activeStage);
      step.classList.toggle("current", index === activeStage);
      if (index === activeStage) step.setAttribute("aria-current", "step");
      else step.removeAttribute("aria-current");
    });
    if (openTab) setTab("post", true);
    persist();
  }

  function filters() {
    const search = $("search").value.trim().toLocaleLowerCase("ru");
    const status = $("status-filter").value;
    const from = $("date-from").value;
    const to = $("date-to").value;
    const invalidRange = Boolean(from && to && from > to);
    $("date-error").hidden = !invalidRange;
    $("date-from").setAttribute("aria-invalid", String(invalidRange));
    $("date-to").setAttribute("aria-invalid", String(invalidRange));
    return posts.filter((post) => {
      const day = dateKey(post.updated_at);
      const matchesStatus = !status || (status.startsWith("schedule:") ? (post.schedule || []).some((schedule) => schedule.status === status.slice(9)) : status === "no-schedule" ? !(post.schedule || []).length : post.status === status);
      return !invalidRange && (!search || searches.get(post.id).includes(search)) && matchesStatus && (!from || day >= from) && (!to || day <= to) && (!state.day || day === state.day);
    });
  }
  function renderHistory() {
    selectControls.get("status-filter")?.refresh();
    const filtered = filters();
    const pages = Math.max(1, Math.ceil(filtered.length / 20));
    state.page = Math.min(Math.max(state.page, 1), pages);
    $("result-count").textContent = `Найдено: ${filtered.length} из ${posts.length}`;
    $("history-empty").hidden = filtered.length > 0;
    $("history-rows").closest(".table-scroll").hidden = filtered.length === 0;
    const rows = filtered.slice((state.page - 1) * 20, state.page * 20).map((post) => {
      const row = document.createElement("tr");
      const titleCell = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "row-title";
      button.textContent = post.title || "Без названия";
      button.addEventListener("click", () => selectPost(post.id, true));
      const sub = document.createElement("span");
      sub.className = "row-subtitle";
      sub.textContent = `Версия ${post.version} · ${post.destination?.chat_title || post.destination?.chat_id || "Чат не выбран"}`;
      titleCell.append(button, sub);
      const statusCell = document.createElement("td");
      statusCell.append(statusNode(post));
      for (const schedule of post.schedule || []) {
        const info = document.createElement("span");
        info.className = "row-subtitle";
        info.append(scheduleStatus(schedule), document.createTextNode(` · ${scheduleDate(schedule)} · версия ${schedule.version}`));
        statusCell.append(info);
      }
      if (post.visual_verified) {
        const visual = document.createElement("span");
        visual.className = "visual-label";
        visual.textContent = "Вид проверен";
        statusCell.append(visual);
      }
      const dateCell = document.createElement("td");
      const time = document.createElement("time");
      time.dateTime = post.updated_at || "";
      time.textContent = formatDate(post.updated_at);
      dateCell.append(time);
      row.append(titleCell, statusCell, dateCell);
      return row;
    });
    $("history-rows").replaceChildren(...rows);
    $("page-info").textContent = `Страница ${state.page} из ${pages}`;
    $("page-prev").disabled = state.page === 1;
    $("page-next").disabled = state.page === pages;
    $("calendar-selection").textContent = state.day ? `Выбран день: ${formatDate(state.day + "T12:00:00")}` : "Выбери день с постами";
    $("clear-day").hidden = !state.day;
  }
  function renderCalendar() {
    $("calendar-title").textContent = new Intl.DateTimeFormat("ru-RU", {month: "long", year: "numeric"}).format(calendarMonth);
    const year = calendarMonth.getFullYear();
    const month = calendarMonth.getMonth();
    const nodes = [];
    const offset = (new Date(year, month, 1).getDay() + 6) % 7;
    for (let i = 0; i < offset; i++) nodes.push(document.createElement("span"));
    const days = new Date(year, month + 1, 0).getDate();
    for (let day = 1; day <= days; day++) {
      const date = new Date(year, month, day);
      const key = dateKey(date);
      const count = dayCounts.get(key) || 0;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = String(day);
      button.disabled = count === 0;
      button.classList.toggle("has-posts", count > 0);
      button.classList.toggle("today", key === dateKey(new Date()));
      button.setAttribute("aria-pressed", String(state.day === key));
      button.setAttribute("aria-label", `${formatDate(date)}, постов: ${count}`);
      button.addEventListener("click", () => {
        state.day = key;
        state.page = 1;
        $("date-from").value = "";
        $("date-to").value = "";
        renderHistory();
        renderCalendar();
      });
      nodes.push(button);
    }
    $("calendar-days").replaceChildren(...nodes);
  }

  $("post-select").replaceChildren(...posts.map((post) => {
    const option = document.createElement("option");
    option.value = post.id;
    option.textContent = post.title || "Без названия";
    return option;
  }));
  $("post-select").disabled = !posts.length;
  enhanceSelect($("post-select"), "Выбранный пост");
  for (const [value, label] of (posts.some((post) => (post.schedule || []).length) ? [["no-schedule", "Без записей эксперимента"], ...Object.entries(scheduleLabels).map(([status, label]) => ["schedule:" + status, "История отложки: " + label])] : [])) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    $("status-filter").append(option);
  }
  enhanceSelect($("status-filter"), "Статус");
  $("empty-posts").hidden = posts.length > 0;
  $("post-content").hidden = posts.length === 0;
  $("post-select").addEventListener("change", (event) => selectPost(event.target.value));
  $("tab-post").addEventListener("click", () => setTab("post"));
  $("tab-history").addEventListener("click", () => setTab("history"));
  document.querySelector(".brand").addEventListener("click", (event) => {event.preventDefault(); setTab("post");});
  $("filters").addEventListener("submit", (event) => event.preventDefault());
  $("filters").addEventListener("input", (event) => {
    state.page = 1;
    if (["date-from", "date-to"].includes(event.target.id)) {state.day = ""; renderCalendar();}
    renderHistory();
  });
  $("reset-filters").addEventListener("click", () => {
    $("filters").reset(); state.day = ""; state.page = 1; renderHistory(); renderCalendar();
  });
  $("clear-day").addEventListener("click", () => {state.day = ""; state.page = 1; renderHistory(); renderCalendar();});
  $("page-prev").addEventListener("click", () => {state.page--; renderHistory();});
  $("page-next").addEventListener("click", () => {state.page++; renderHistory();});
  $("month-prev").addEventListener("click", () => {calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() - 1, 1); renderCalendar();});
  $("month-next").addEventListener("click", () => {calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + 1, 1); renderCalendar();});
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pauseMedia(document);
    else refreshScheduleDeadlines();
  });
  setInterval(refreshScheduleDeadlines, 1000);
  window.addEventListener("pagehide", () => pauseMedia(document));
  if (posts.length) selectPost(state.postId);
  renderHistory();
  renderCalendar();
  setTab(state.tab);
})();
