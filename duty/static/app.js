/* Веб-интерфейс сервиса дежурств: FullCalendar + REST API /api/v1 */
"use strict";

const API = "/api/v1";
const COLORS = ["#0f6cbd", "#0e7a5f", "#8a3ffc", "#b35c00", "#007c89", "#a2287a", "#4f6bed", "#5c7a00"];
const KINDS = { vacation: "отпуск", sick: "больничный", dayoff: "отгул", other: "другое" };
const REASONS = {
  new: "новая смена", absent: "отсутствие", inactive: "выключен из ротации", unfilled: "место было свободно",
  rebalance: "перебалансировка", removed: "смена удалена", manual: "вручную", swap: "обмен", unassigned: "некому дежурить",
};

const state = { me: null, teams: [], people: [], selected: new Set(), calendar: null, data: null, swap: null };

/* ---------------------------------------------------------------- утилиты */

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = new Intl.DateTimeFormat("ru", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const fmtDay = new Intl.DateTimeFormat("ru", { day: "2-digit", month: "2-digit", year: "numeric" });
const range = (a, b) => `${fmt.format(new Date(a))} – ${fmt.format(new Date(b))}`;
const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function store(key, value) {
  try { value === undefined ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* нет хранилища */ }
}
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (_) { return fallback; }
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body, params) {
  const url = new URL(API + path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null) continue;
    (Array.isArray(v) ? v : [v]).forEach((x) => url.searchParams.append(k, x));
  }
  const res = await fetch(url, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "/auth/login") { showLogin(); throw new ApiError(401, "нужно войти"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new ApiError(res.status, msg || res.statusText);
  }
  return data;
}

function toast(text, isError = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (isError ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), isError ? 6000 : 3000);
}

async function guard(fn) {
  try { return await fn(); } catch (e) { if (e.status !== 401) toast(e.message, true); throw e; }
}

/* ---------------------------------------------------------------- диалог */

function openDialog(title, contentHtml, buttons = []) {
  const dlg = $("#dlg");
  $("#dlg-body").innerHTML = `
    <div class="dlg-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Закрыть">×</button></div>
    <div class="dlg-content">${contentHtml}</div>
    <div class="dlg-foot"></div>`;
  const foot = $("#dlg-body .dlg-foot");
  for (const b of buttons) {
    const el = document.createElement("button");
    el.className = "btn " + (b.cls || "");
    el.textContent = b.text;
    el.onclick = () => b.onClick(el);
    foot.appendChild(el);
  }
  if (!buttons.length) foot.hidden = true;
  $("#dlg-body [data-close]").onclick = () => dlg.close();
  if (!dlg.open) dlg.showModal();
  return $("#dlg-body .dlg-content");
}
const closeDialog = () => $("#dlg").close();

/* ---------------------------------------------------------------- вход */

function showLogin() {
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("POST", "/auth/login", { username: f.get("username"), password: f.get("password") });
    $("#login-error").hidden = true;
    await start();
  } catch (err) {
    $("#login-error").textContent = err.message;
    $("#login-error").hidden = false;
  }
});

$("#btn-logout").onclick = async () => { await api("POST", "/auth/logout"); location.reload(); };
$("#btn-user").onclick = () => ($("#user-menu").hidden = !$("#user-menu").hidden);
document.addEventListener("click", (e) => { if (!e.target.closest(".user-menu")) $("#user-menu").hidden = true; });
$("#toggle-side").onclick = () => $("#side").classList.toggle("open");

/* ---------------------------------------------------------------- данные */

function teamColor(slug) {
  const i = state.teams.findIndex((t) => t.slug === slug);
  return COLORS[(i < 0 ? 0 : i) % COLORS.length];
}
const teamBySlug = (slug) => state.teams.find((t) => t.slug === slug);
const personById = (id) => state.people.find((p) => p.id === id);

async function loadReference() {
  [state.teams, state.people] = await Promise.all([api("GET", "/teams"), api("GET", "/people")]);
  const saved = load("duty.teams", null);
  state.selected = new Set((saved || state.teams.map((t) => t.slug)).filter((s) => teamBySlug(s)));
  if (!state.selected.size) state.teams.forEach((t) => state.selected.add(t.slug));
  renderTeamFilter();
}

function renderTeamFilter() {
  $("#team-filter").innerHTML = state.teams.map((t) => `
    <label class="inline"><input type="checkbox" value="${esc(t.slug)}" ${state.selected.has(t.slug) ? "checked" : ""}>
      <span><span class="team-dot" style="background:${teamColor(t.slug)}"></span>${esc(t.name)}</span></label>`).join("")
    || `<p class="muted">Команд пока нет${state.me.role === "admin" ? " — создайте в разделе «Команды»" : ""}.</p>`;
  $("#team-filter").querySelectorAll("input").forEach((cb) => cb.onchange = () => {
    cb.checked ? state.selected.add(cb.value) : state.selected.delete(cb.value);
    store("duty.teams", [...state.selected]);
    refresh();
  });
}

async function renderOncall() {
  const items = await Promise.all([...state.selected].map(async (slug) => {
    const shifts = await api("GET", `/teams/${slug}/oncall`).catch(() => []);
    const t = teamBySlug(slug);
    if (!shifts.length) return `<div class="oc"><div class="t">${esc(t.name)}</div><div class="p muted">нет смены</div></div>`;
    const sh = shifts[0];
    const who = sh.seats.map((s) => s.person?.name).filter(Boolean).join(", ") || "никто";
    return `<div class="oc"><div class="t">${esc(t.name)} · до ${esc(fmt.format(new Date(sh.end)))}</div><div class="p">${esc(who)}</div></div>`;
  }));
  $("#oncall-list").innerHTML = items.join("") || `<p class="muted">—</p>`;
}

function absencesOf(personId, start, end) {
  const s = new Date(start), e = new Date(end);
  return (state.data?.absences || []).filter((a) => a.person.id === personId && new Date(a.start) < e && new Date(a.end) > s);
}

function shiftProblems(sh) {
  const problems = [];
  const filled = sh.seats.filter((s) => s.person);
  if (filled.length < sh.people_per_shift) problems.push("не все места заняты");
  for (const s of filled) if (absencesOf(s.person.id, sh.start, sh.end).length) problems.push(`${s.person.name} отсутствует`);
  return problems;
}

function toEvents(data) {
  const events = [];
  for (const sh of data.shifts) {
    const names = sh.seats.map((s) => s.person?.name || "—").join(", ");
    const cls = [];
    if (sh.seats.some((s) => s.pinned)) cls.push("ev-pinned");
    if (shiftProblems(sh).length) cls.push("ev-problem");
    if (state.swap && state.swap.shift.id === sh.id) cls.push("ev-selecting");
    const color = teamColor(sh.team);
    events.push({
      id: "s" + sh.id, title: (state.selected.size > 1 ? sh.team_name + ": " : "") + names + (cls.includes("ev-pinned") ? " 📌" : ""),
      start: sh.start, end: sh.end, backgroundColor: color, borderColor: color, classNames: cls,
      display: "block", extendedProps: { type: "shift", shift: sh },
    });
  }
  for (const a of data.absences) {
    const s = new Date(a.start), e = new Date(a.end);
    const wholeDays = s.getHours() === 0 && s.getMinutes() === 0 && e.getHours() === 0 && e.getMinutes() === 0;
    events.push({
      id: "a" + a.id, title: `🏖 ${a.person.name} · ${KINDS[a.kind] || a.kind}`,
      start: wholeDays ? isoDate(s) : a.start, end: wholeDays ? isoDate(e) : a.end, allDay: wholeDays,
      classNames: ["ev-absence"], display: "block", extendedProps: { type: "absence", absence: a },
    });
  }
  for (const h of data.holidays) {
    if (h.kind === "holiday" || h.kind === "dayoff") {
      events.push({ start: h.day, allDay: true, display: "background", title: h.name, classNames: ["ev-holiday"] });
    }
  }
  return events;
}

/* ---------------------------------------------------------------- календарь */

function initCalendar() {
  state.calendar = new FullCalendar.Calendar($("#calendar"), {
    locale: "ru", firstDay: 1, height: "auto",
    initialView: load("duty.view", window.innerWidth < 800 ? "listMonth" : "dayGridMonth"),
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listMonth" },
    views: { listMonth: { buttonText: "Список" } },
    eventTimeFormat: { hour: "2-digit", minute: "2-digit", hour12: false },
    displayEventEnd: false, dayMaxEvents: 6, nowIndicator: true,
    eventOrder: (a, b) => (a.extendedProps.type === "shift" ? 0 : 1) - (b.extendedProps.type === "shift" ? 0 : 1),
    datesSet: (info) => store("duty.view", info.view.type),
    events: async (info, success, failure) => {
      try {
        if (!state.selected.size) { state.data = { shifts: [], absences: [], holidays: [] }; return success([]); }
        state.data = await api("GET", "/calendar", undefined, {
          start: info.start.toISOString(), end: info.end.toISOString(), teams: [...state.selected],
        });
        success(toEvents(state.data));
      } catch (e) { failure(e); }
    },
    eventClick: (info) => {
      const p = info.event.extendedProps;
      if (p.type === "shift") (state.swap ? finishSwap(p.shift) : openShift(p.shift.id));
      else if (p.type === "absence") openAbsence(p.absence);
    },
  });
  state.calendar.render();
}

function refresh() {
  state.calendar?.refetchEvents();
  renderOncall();
}

/* ---------------------------------------------------------------- смена */

function memberOptions(team, sh, current) {
  const inShift = new Set(sh.seats.map((s) => s.person?.id).filter(Boolean));
  const opts = [`<option value="">— свободно —</option>`];
  for (const m of team.members) {
    const person = state.people.find((p) => p.email === m.email);
    if (!person) continue;
    const marks = [];
    if (!m.active) marks.push("выключен");
    if (absencesOf(person.id, sh.start, sh.end).length) marks.push("отсутствует");
    if (inShift.has(person.id) && person.id !== current) marks.push("уже в смене");
    opts.push(`<option value="${esc(m.email)}" ${person.id === current ? "selected" : ""}>${esc(m.name)}${marks.length ? " · " + marks.join(", ") : ""}</option>`);
  }
  return opts.join("");
}

async function assignWithForce(shiftId, email, seat) {
  try {
    return await api("POST", `/shifts/${shiftId}/assign`, { email, seat, force: false });
  } catch (e) {
    if (e.status === 409 && /force/.test(e.message) && confirm(e.message.replace(/Чтобы.*|Повторите.*/, "") + "\n\nНазначить всё равно?")) {
      return api("POST", `/shifts/${shiftId}/assign`, { email, seat, force: true });
    }
    throw e;
  }
}

async function openShift(shiftId) {
  const sh = await guard(() => api("GET", `/shifts/${shiftId}`));
  const team = teamBySlug(sh.team) || (await api("GET", `/teams/${sh.team}`));
  const past = new Date(sh.end) <= new Date();
  const seatCount = Math.max(sh.people_per_shift, ...sh.seats.map((s) => s.seat + 1), 0);
  const seats = [];
  for (let i = 0; i < seatCount; i++) {
    const s = sh.seats.find((x) => x.seat === i) || { seat: i, person: null, pinned: false };
    seats.push(`
      <div class="seat" data-seat="${i}">
        <div>
          <select ${past ? "disabled" : ""}>${memberOptions(team, sh, s.person?.id)}</select>
          ${s.pinned ? `<div class="pin">📌 закреплено вручную — планировщик не трогает</div>` : ""}
          ${i >= sh.people_per_shift ? `<div class="muted">дополнительное место</div>` : ""}
        </div>
        <div class="acts">
          ${past ? "" : `<button class="btn small primary" data-act="assign">Назначить</button>`}
          ${!past && s.pinned ? `<button class="btn small" data-act="unpin">Открепить</button>` : ""}
          ${!past && s.person ? `<button class="btn small" data-act="swap">Обменять</button>` : ""}
          ${!past && s.person ? `<button class="btn small danger" data-act="free">Освободить</button>` : ""}
        </div>
      </div>`);
  }
  const problems = shiftProblems(sh);
  const body = openDialog(`${sh.team_name}: смена`, `
    <div><b>${esc(range(sh.start, sh.end))}</b></div>
    ${past ? `<p class="muted">Смена уже прошла — только просмотр.</p>` : ""}
    ${problems.length ? `<ul class="changes">${problems.map((p) => `<li class="warn">⚠️ ${esc(p)}</li>`).join("")}</ul>` : ""}
    ${seats.join("")}
    ${past ? "" : `<button class="btn ghost" id="add-seat">+ добавить дежурного сверх нормы</button>`}
    <details><summary class="muted">История изменений</summary><div class="log" id="shift-log">загрузка…</div></details>
  `);

  body.querySelectorAll(".seat").forEach((row) => {
    const seat = Number(row.dataset.seat);
    const email = () => row.querySelector("select").value || null;
    row.querySelectorAll("button").forEach((btn) => btn.onclick = () => guard(async () => {
      const act = btn.dataset.act;
      if (act === "assign") {
        if (!email()) return toast("Выберите человека", true);
        await assignWithForce(sh.id, email(), seat);
        toast("Назначено и закреплено");
      } else if (act === "unpin") {
        await api("POST", `/shifts/${sh.id}/unpin`, { seat });
        toast("Закрепление снято, планировщик проверил смену");
      } else if (act === "free") {
        await api("POST", `/shifts/${sh.id}/assign`, { email: null, seat });
        toast("Место освобождено");
      } else if (act === "swap") {
        state.swap = { shift: sh, seat };
        $("#swap-banner").hidden = false;
        closeDialog();
        state.calendar.refetchEvents();
        return;
      }
      closeDialog();
      refresh();
    }));
  });

  const addBtn = body.querySelector("#add-seat");
  if (addBtn) addBtn.onclick = () => {
    addBtn.outerHTML = `<div class="row"><select id="extra-sel">${memberOptions(team, sh, null)}</select>
      <button class="btn primary" id="extra-ok" style="flex:0">Добавить</button></div>`;
    $("#extra-ok").onclick = () => guard(async () => {
      const em = $("#extra-sel").value;
      if (!em) return toast("Выберите человека", true);
      await assignWithForce(sh.id, em, null);
      closeDialog();
      refresh();
    });
  };

  api("GET", `/shifts/${sh.id}/log`).then((rows) => {
    const name = (id) => (id ? personById(id)?.name || `#${id}` : "—");
    $("#shift-log").innerHTML = rows.length ? rows.map((r) =>
      `<div>${esc(fmt.format(new Date(r.at)))} · место ${r.seat + 1}: ${esc(name(r.old_person_id))} → ${esc(name(r.new_person_id))} (${esc(REASONS[r.reason] || r.reason)}, ${esc(r.actor)})</div>`).join("")
      : "пусто";
  }).catch(() => {});
}

async function finishSwap(target) {
  const src = state.swap;
  if (target.id === src.shift.id) return;
  const filled = target.seats.filter((s) => s.person);
  if (!filled.length) return toast("В выбранной смене никого нет", true);
  const doSwap = async (seatB, force = false) => {
    try {
      await api("POST", "/shifts/swap", { shift_a: src.shift.id, seat_a: src.seat, shift_b: target.id, seat_b: seatB, force });
    } catch (e) {
      if (e.status === 409 && /force/.test(e.message) && confirm(e.message + "\n\nПоменять всё равно?")) return doSwap(seatB, true);
      throw e;
    }
    cancelSwap();
    closeDialog();
    toast("Смены обменяны");
    refresh();
  };
  if (filled.length === 1) return guard(() => doSwap(filled[0].seat));
  const body = openDialog("С кем поменяться?", filled.map((s) =>
    `<button class="btn" data-seat="${s.seat}">${esc(s.person.name)}</button>`).join(""));
  body.querySelectorAll("button").forEach((b) => b.onclick = () => guard(() => doSwap(Number(b.dataset.seat))));
}

function cancelSwap() {
  state.swap = null;
  $("#swap-banner").hidden = true;
  state.calendar.refetchEvents();
}
$("#swap-cancel").onclick = cancelSwap;

/* ---------------------------------------------------------------- планы */

function renderReports(reports) {
  const parts = [];
  for (const r of reports) {
    const items = r.changes.filter((c) => c.reason !== "new").map((c) =>
      `<li>${esc(range(c.start, c.end))}${c.seat ? ` · место ${c.seat + 1}` : ""}: ${esc(c.old?.name || "—")} → <b>${esc(c.new?.name || "—")}</b> <span class="muted">(${esc(REASONS[c.reason] || c.reason)})</span></li>`);
    const conflicts = r.conflicts.map((c) =>
      `<li class="warn">⚠️ ${esc(range(c.start, c.end))}: ${c.reason === "unassigned" ? "некому дежурить" : `${esc(c.person?.name || "")} — ${esc(REASONS[c.reason] || c.reason)}, нужна ручная замена`}</li>`);
    const newCount = r.changes.filter((c) => c.reason === "new").length;
    parts.push(`<div><b>${esc(r.team_name)}</b>${r.dry_run ? ' <span class="muted">(предпросмотр)</span>' : ""}
      <ul class="changes">${items.join("") || "<li>Перестановок нет</li>"}${conflicts.join("")}</ul>
      ${newCount ? `<p class="muted">Новых смен: ${newCount}</p>` : ""}</div>`);
  }
  return parts.join("") || `<p class="muted">Человек не состоит ни в одной команде — график не меняется.</p>`;
}

/* ---------------------------------------------------------------- отсутствия */

function absenceForm() {
  const today = isoDate(new Date());
  const people = [...state.people].sort((a, b) => a.name.localeCompare(b.name, "ru"));
  return `
    <label>Кто<select id="ab-person">${people.map((p) => `<option value="${esc(p.email)}">${esc(p.name)} (${esc(p.email)})</option>`).join("")}</select></label>
    <div class="grid2">
      <label>С (включительно)<input type="date" id="ab-start" value="${today}"></label>
      <label>По (включительно)<input type="date" id="ab-end" value="${today}"></label>
    </div>
    <label>Тип<select id="ab-kind">${Object.entries(KINDS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
    <label>Комментарий<input id="ab-note" maxlength="300"></label>
    <div id="ab-preview"></div>`;
}

function openAddAbsence() {
  if (!state.people.length) return toast("Сначала добавьте людей в команды", true);
  const payload = () => ({ email: $("#ab-person").value, start: $("#ab-start").value, end: $("#ab-end").value,
                           kind: $("#ab-kind").value, note: $("#ab-note").value });
  openDialog("Новое отсутствие", absenceForm(), [
    { text: "Что изменится?", onClick: () => guard(async () => {
      const r = await api("POST", "/absences", payload(), { dry_run: true });
      $("#ab-preview").innerHTML = renderReports(r.plans);
    }) },
    { text: "Сохранить", cls: "primary", onClick: () => guard(async () => {
      const r = await api("POST", "/absences", payload());
      refresh();
      openDialog("Отсутствие сохранено", renderReports(r.plans), [{ text: "OK", cls: "primary", onClick: closeDialog }]);
    }) },
  ]);
}
$("#btn-absence").onclick = openAddAbsence;

function openAbsence(a) {
  const endIncl = new Date(new Date(a.end).getTime() - 1);
  openDialog("Отсутствие", `
    <div><b>${esc(a.person.name)}</b> · ${esc(KINDS[a.kind] || a.kind)}</div>
    <div>${esc(fmtDay.format(new Date(a.start)))} – ${esc(fmtDay.format(endIncl))}</div>
    ${a.note ? `<div>${esc(a.note)}</div>` : ""}
    <div class="muted">Источник: ${esc(a.source)}${a.created_by ? `, добавил ${esc(a.created_by)}` : ""}</div>
    <div id="ab-preview"></div>`, [
    { text: "Что изменится при удалении?", onClick: () => guard(async () => {
      const r = await api("DELETE", `/absences/${a.id}`, undefined, { dry_run: true });
      $("#ab-preview").innerHTML = renderReports(r.plans);
    }) },
    { text: "Удалить", cls: "danger", onClick: () => guard(async () => {
      if (!confirm("Удалить отсутствие?")) return;
      const r = await api("DELETE", `/absences/${a.id}`);
      refresh();
      openDialog("Отсутствие удалено", renderReports(r.plans), [{ text: "OK", cls: "primary", onClick: closeDialog }]);
    }) },
  ]);
}

/* ---------------------------------------------------------------- перепланирование */

$("#btn-replan").onclick = () => {
  if (!state.teams.length) return toast("Команд пока нет", true);
  openDialog("Перепланировать", `
    <label>Команда<select id="rp-team">${state.teams.map((t) => `<option value="${esc(t.slug)}">${esc(t.name)}</option>`).join("")}</select></label>
    <label class="inline"><input type="radio" name="rp-mode" value="fix" checked> Исправить — заменить только тех, кто не может дежурить</label>
    <label class="inline"><input type="radio" name="rp-mode" value="rebalance"> Перебалансировать — пересчитать будущее (кроме закреплённых и ближайших дней)</label>
    <div id="rp-preview"></div>`, [
    { text: "Предпросмотр", onClick: () => guard(async () => {
      const r = await api("POST", `/teams/${$("#rp-team").value}/plan`, undefined,
        { mode: document.querySelector("[name=rp-mode]:checked").value, dry_run: true });
      $("#rp-preview").innerHTML = renderReports([r]);
    }) },
    { text: "Применить", cls: "primary", onClick: () => guard(async () => {
      const r = await api("POST", `/teams/${$("#rp-team").value}/plan`, undefined,
        { mode: document.querySelector("[name=rp-mode]:checked").value });
      refresh();
      openDialog("Готово", renderReports([r]), [{ text: "OK", cls: "primary", onClick: closeDialog }]);
    }) },
  ]);
};

/* ---------------------------------------------------------------- токены */

$("#btn-tokens").onclick = async () => {
  $("#user-menu").hidden = true;
  const tokens = await guard(() => api("GET", "/me/tokens"));
  const body = openDialog("Токены для API, MCP и подписки на календарь", `
    <p class="muted">Личный токен заменяет вход: <code>Authorization: Bearer &lt;токен&gt;</code>. MCP-сервер: <code>${esc(location.origin)}/mcp/</code></p>
    <table class="simple"><tr><th>Название</th><th>Создан</th><th>Последнее использование</th><th></th></tr>
      ${tokens.map((t) => `<tr><td>${esc(t.name)}</td><td>${esc(fmtDay.format(new Date(t.created_at)))}</td>
        <td>${t.last_used_at ? esc(fmt.format(new Date(t.last_used_at))) : "—"}</td>
        <td><button class="btn small danger" data-id="${t.id}">Отозвать</button></td></tr>`).join("") || `<tr><td colspan="4" class="muted">нет токенов</td></tr>`}
    </table>
    <div class="row"><label>Название нового токена<input id="tk-name" value="mcp"></label>
      <button class="btn primary" id="tk-create" style="flex:0">Создать</button></div>
    <div id="tk-result"></div>`);
  body.querySelectorAll("[data-id]").forEach((b) => b.onclick = () => guard(async () => {
    await api("DELETE", `/me/tokens/${b.dataset.id}`);
    $("#btn-tokens").click();
  }));
  $("#tk-create").onclick = () => guard(async () => {
    const t = await api("POST", "/me/tokens", { name: $("#tk-name").value || "token" });
    const ics = state.teams.map((tm) => `<div>${esc(tm.name)}: <span class="tokenbox">${esc(location.origin)}/api/v1/teams/${esc(tm.slug)}/calendar.ics?token=${esc(t.token)}</span></div>`).join("");
    $("#tk-result").innerHTML = `
      <p><b>Скопируйте токен сейчас — больше он не будет показан:</b></p>
      <div class="tokenbox">${esc(t.token)}</div>
      <p class="muted">Пример подключения MCP (Claude Desktop / Claude Code):</p>
      <pre class="tokenbox">${esc(JSON.stringify({ mcpServers: { duty: { type: "http", url: location.origin + "/mcp/", headers: { Authorization: "Bearer " + t.token } } } }, null, 2))}</pre>
      ${ics ? `<p class="muted">Подписка на календарь (ICS):</p>${ics}` : ""}`;
  });
};

/* ---------------------------------------------------------------- администрирование */

const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function teamForm(t) {
  const v = (k, d = "") => esc(t?.[k] ?? d);
  const wd = t?.weekdays ?? "1234567";
  return `
    <div class="grid2">
      ${t ? "" : `<label>Код (латиница, для URL)<input id="tf-slug" placeholder="backend"></label>`}
      <label>Название<input id="tf-name" value="${v("name")}"></label>
      <label>Длина смены, дней<input id="tf-len" type="number" min="1" value="${v("shift_length_days", 7)}"></label>
      <label>Дежурных на смену<input id="tf-ppl" type="number" min="1" max="20" value="${v("people_per_shift", 1)}"></label>
      <label>Время передачи смены<input id="tf-hand" type="time" value="${v("handoff_time", "10:00")}"></label>
      <label>Дата начала сетки<input id="tf-anchor" type="date" value="${v("anchor_date", isoDate(new Date()))}"></label>
      <label>Часовой пояс<input id="tf-tz" value="${v("timezone", "Europe/Moscow")}"></label>
      <label>Планировать на N дней<input id="tf-hor" type="number" min="1" value="${v("horizon_days", 56)}"></label>
      <label>Не трогать при перебалансировке, дней<input id="tf-frz" type="number" min="0" value="${v("freeze_days", 7)}"></label>
      <label>Сообщать о перестановках на N дней<input id="tf-ntf" type="number" min="0" value="${v("notify_days", 14)}"></label>
      <label>Частичное отсутствие<select id="tf-pol">
        <option value="whole" ${wd && t?.absence_policy !== "split" ? "selected" : ""}>переназначить всю смену</option>
        <option value="split" ${t?.absence_policy === "split" ? "selected" : ""}>делить смену (ещё не реализовано)</option></select></label>
      <label>Mattermost webhook<input id="tf-mm" value="${v("mattermost_webhook")}" placeholder="https://mm/hooks/..."></label>
      <label>Канал Mattermost (необязательно)<input id="tf-mmc" value="${v("mattermost_channel")}"></label>
    </div>
    <div><span class="muted">Дни начала смены:</span>
      ${WD.map((d, i) => `<label class="inline" style="display:inline-flex;margin-right:8px"><input type="checkbox" class="tf-wd" value="${i + 1}" ${wd.includes(String(i + 1)) ? "checked" : ""}>${d}</label>`).join("")}</div>
    <label class="inline"><input type="checkbox" id="tf-skip" ${t?.skip_holidays ? "checked" : ""}> Не начинать смены в праздники и выходные по производственному календарю</label>`;
}

function teamPayload() {
  const num = (id) => Number($(id).value);
  return {
    name: $("#tf-name").value, shift_length_days: num("#tf-len"), people_per_shift: num("#tf-ppl"),
    handoff_time: $("#tf-hand").value, anchor_date: $("#tf-anchor").value, timezone: $("#tf-tz").value,
    horizon_days: num("#tf-hor"), freeze_days: num("#tf-frz"), notify_days: num("#tf-ntf"),
    absence_policy: $("#tf-pol").value, mattermost_webhook: $("#tf-mm").value || null,
    mattermost_channel: $("#tf-mmc").value || null, skip_holidays: $("#tf-skip").checked,
    weekdays: [...document.querySelectorAll(".tf-wd:checked")].map((c) => c.value).join("") || "1234567",
  };
}

async function reloadAll() { await loadReference(); refresh(); }

async function openAdmin() {
  await loadReference();
  const users = await api("GET", "/users").catch(() => []);
  const body = openDialog("Команды и пользователи", `
    <table class="simple"><tr><th>Команда</th><th>Ротация</th><th>Участников</th><th></th></tr>
      ${state.teams.map((t) => `<tr><td>${esc(t.name)}</td><td>${t.shift_length_days} дн. × ${t.people_per_shift} чел.</td>
        <td>${t.members.filter((m) => m.active).length}/${t.members.length}</td>
        <td><button class="btn small" data-team="${esc(t.slug)}">Настроить</button></td></tr>`).join("")}
    </table>
    <button class="btn" id="adm-new-team">+ Новая команда</button>
    <h3 style="margin-top:12px">Пользователи сервиса</h3>
    <table class="simple"><tr><th>Логин</th><th>Роль</th><th>Активен</th><th></th></tr>
      ${users.map((u) => `<tr><td>${esc(u.username)}</td><td>${u.role === "admin" ? "админ" : "пользователь"}</td>
        <td>${u.active ? "да" : "нет"}</td><td><button class="btn small" data-user="${esc(u.username)}" data-active="${u.active}">${u.active ? "Отключить" : "Включить"}</button></td></tr>`).join("")}
    </table>
    <div class="row"><label>Логин<input id="nu-name"></label><label>Пароль<input id="nu-pass" type="password"></label>
      <label>Роль<select id="nu-role"><option value="user">пользователь</option><option value="admin">админ</option></select></label>
      <button class="btn primary" id="nu-create" style="flex:0">Создать</button></div>
    <button class="btn ghost" id="adm-holidays">Обновить производственный календарь</button>`);
  body.querySelectorAll("[data-team]").forEach((b) => b.onclick = () => openTeamEditor(b.dataset.team));
  body.querySelectorAll("[data-user]").forEach((b) => b.onclick = () => guard(async () => {
    await api("PATCH", `/users/${encodeURIComponent(b.dataset.user)}`, { active: b.dataset.active !== "true" });
    openAdmin();
  }));
  $("#adm-new-team").onclick = () => openTeamEditor(null);
  $("#nu-create").onclick = () => guard(async () => {
    await api("POST", "/users", { username: $("#nu-name").value, password: $("#nu-pass").value, role: $("#nu-role").value });
    toast("Пользователь создан");
    openAdmin();
  });
  $("#adm-holidays").onclick = () => guard(async () => {
    const r = await api("POST", "/holidays/sync");
    toast(`Загружено дней: ${r.loaded} (${r.provider})`);
    refresh();
  });
}
$("#btn-admin").onclick = () => guard(openAdmin);

async function openTeamEditor(slug) {
  const t = slug ? await api("GET", `/teams/${slug}`) : null;
  const members = t ? t.members.map((m, i) => `
    <tr data-email="${esc(m.email)}"><td>${esc(m.name)}<div class="muted">${esc(m.email)}${m.mattermost_username ? " · @" + esc(m.mattermost_username) : ""}</div></td>
      <td><label class="inline"><input type="checkbox" class="mb-active" ${m.active ? "checked" : ""}>в ротации</label></td>
      <td><button class="btn small mb-up" ${i === 0 ? "disabled" : ""}>↑</button><button class="btn small mb-down" ${i === t.members.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn small danger mb-del">×</button></td></tr>`).join("") : "";
  const body = openDialog(t ? `Команда «${t.name}»` : "Новая команда", `
    ${teamForm(t)}
    ${t ? `<h3>Участники (порядок = начальная очередь)</h3>
      <table class="simple">${members || `<tr><td class="muted">пока никого</td></tr>`}</table>
      <div class="grid2"><label>Email<input id="mb-email"></label><label>Имя<input id="mb-name"></label>
        <label>Логин Mattermost<input id="mb-mm"></label><div class="row"><button class="btn" id="mb-add">Добавить участника</button></div></div>` : ""}`, [
    ...(t ? [{ text: "Удалить команду", cls: "danger", onClick: () => guard(async () => {
      if (!confirm(`Удалить команду «${t.name}» вместе со всеми сменами?`)) return;
      await api("DELETE", `/teams/${t.slug}`);
      await reloadAll();
      openAdmin();
    }) }] : []),
    { text: "Сохранить", cls: "primary", onClick: () => guard(async () => {
      if (t) await api("PATCH", `/teams/${t.slug}`, teamPayload());
      else await api("POST", "/teams", { slug: $("#tf-slug").value, ...teamPayload() });
      await reloadAll();
      toast("Сохранено, график перепланирован");
      openTeamEditor(t ? t.slug : $("#tf-slug")?.value || null);
    }) },
  ]);
  if (!t) return;
  const emails = t.members.map((m) => m.email);
  body.querySelectorAll("tr[data-email]").forEach((row) => {
    const email = row.dataset.email;
    const enc = encodeURIComponent(email);
    row.querySelector(".mb-active").onchange = (e) => guard(async () => {
      await api("PATCH", `/teams/${t.slug}/members/${enc}`, { active: e.target.checked });
      await reloadAll();
    });
    row.querySelector(".mb-del").onclick = () => guard(async () => {
      if (!confirm(`Убрать ${email} из команды?`)) return;
      await api("DELETE", `/teams/${t.slug}/members/${enc}`);
      await reloadAll();
      openTeamEditor(t.slug);
    });
    const move = (d) => guard(async () => {
      const i = emails.indexOf(email);
      const order = [...emails];
      [order[i], order[i + d]] = [order[i + d], order[i]];
      await api("PUT", `/teams/${t.slug}/members`, order);
      openTeamEditor(t.slug);
    });
    row.querySelector(".mb-up").onclick = () => move(-1);
    row.querySelector(".mb-down").onclick = () => move(1);
  });
  $("#mb-add").onclick = () => guard(async () => {
    await api("POST", `/teams/${t.slug}/members`, {
      email: $("#mb-email").value, name: $("#mb-name").value || null, mattermost_username: $("#mb-mm").value || null,
    });
    await reloadAll();
    openTeamEditor(t.slug);
  });
}

/* ---------------------------------------------------------------- старт */

async function start() {
  try {
    state.me = await api("GET", "/me");
  } catch (_) {
    return showLogin();
  }
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#btn-user").textContent = `${state.me.display_name || state.me.username} ▾`;
  $("#btn-admin").hidden = state.me.role !== "admin";
  await loadReference();
  if (!state.calendar) initCalendar(); else state.calendar.refetchEvents();
  renderOncall();
  const m = location.hash.match(/^#shift-(\d+)$/);
  if (m) openShift(Number(m[1])).catch(() => {});
}

start();
