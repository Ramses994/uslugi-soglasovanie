// Логика формы. Без сборщика — чистый скрипт под Alpine.js, чтобы Cursor/разработчик
// мог сразу править и запускать без npm install / webpack.

const API_BASE = ""; // фронтенд раздаётся тем же FastAPI-приложением (см. main.py, StaticFiles на "/")
// Если фронтенд разворачивается отдельно (Nginx/CDN) — укажите здесь адрес бэкенда,
// например: const API_BASE = "https://price-form.clinic.local";

// FastAPI при 422 (ошибка валидации pydantic) отдаёт body.detail как МАССИВ
// объектов {loc, msg, type} — не строку. `new Error(arrayOfObjects)` в JS
// приводит массив к строке через Array.prototype.toString(), а каждый объект
// внутри — через Object.prototype.toString(), что даёт буквально
// "[object Object],[object Object]" в интерфейсе (воспроизведено и найдено
// при реальной проверке формы, см. ТЗ.md). Превращаем detail в читаемый текст
// независимо от того, строка это, список ошибок валидации или что-то ещё.
function formatApiErrorDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") {
          const field = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(".") : "";
          return field ? `${field}: ${item.msg}` : (item.msg || JSON.stringify(item));
        }
        return String(item);
      })
      .join("; ");
  }
  if (typeof detail === "object") return JSON.stringify(detail);
  return String(detail);
}

async function api(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiErrorDetail(body.detail) || `Ошибка запроса: ${res.status}`);
  }
  // 204 No Content (например, DELETE) — тела нет, res.json() упал бы на пустой строке.
  if (res.status === 204) return null;
  return res.json();
}

function emptyForm() {
  return {
    request_type: "new_service",
    name: "",
    mis_code: "",
    service_type: "simple",
    price_list: "both",
    all_branches: false,
    prices: [],
    short_name: "",
    alt_name: "",
    description: "",
    treatment_standard: "",
    date_start: "",
    date_end: "",
    sex_limit: "all",
    age_from: null,
    age_to: null,
    repeat_control_per_day: false,
    is_lab_service: false,
    lab: { erlu_code: "", erlu_name: "", lab_code: "", lab_name: "", direction_name: "", biomaterial_type: "", is_composite: false },
    minzdrav: null,
    business: { service_kind: "", duration_minutes: null, staff_involved: "", equipment: "", consumables: "", medications: "", description: "", comment: "" },
    approver_ids: [],
    submission_comment: "",
  };
}

function app() {
  return {
    // ---- состояние ----
    user: null,
    loading: false,
    loginForm: { login: "", password: "" },

    // ---- всплывающее окно ошибок/системных сообщений ----
    // Раньше ошибки выводились инлайн-текстом под формой (loginError/submitError/
    // adminError) — легко было проскроллить мимо, а массив ошибок валидации FastAPI
    // рендерился как "[object Object],[object Object]" (см. formatApiErrorDetail
    // выше). Теперь единая точка показа — модальное окно поверх интерфейса.
    notice: { open: false, kind: "error", title: "", lines: [] },
    notify(kind, title, message) {
      const lines = Array.isArray(message) ? message : String(message || "").split("; ").filter(Boolean);
      this.notice = { open: true, kind, title, lines: lines.length ? lines : [String(message || "")] };
    },
    closeNotice() {
      this.notice.open = false;
    },

    form: emptyForm(),
    editingId: null,
    branches: [],
    approvers: [],
    requestTypes: [
      { value: "new_service", label: "Новая услуга" },
      { value: "complex_service", label: "Комплексная услуга" },
      { value: "delete", label: "Удаление услуги" },
      { value: "promo", label: "Акция" },
    ],
    serviceTypes: [
      { value: "simple", label: "Простая" },
      { value: "complex", label: "Сложная" },
      { value: "combined", label: "Комплексная" },
      { value: "program", label: "Медпрограмма" },
      { value: "package", label: "Пакет услуг" },
      { value: "prn", label: "ПРН" },
    ],

    uniformPrice: null,
    showOptional: false,
    showBusiness: true,

    minzdravQuery: "",
    minzdravResults: [],
    _minzdravTimer: null,
    _minzdravAutoTimer: null,

    erluQuery: "",
    erluResults: [],
    _erluTimer: null,

    submitted: false,
    submittedId: null,

    // ---- вкладка «Мои заявки» ----
    activeTab: "new", // 'new' | 'my' | 'admin'
    myRequests: [],
    myRequestsLoading: false,
    myFilters: { status: "", request_type: "", q: "" },
    expandedId: null,
    requestDetails: {}, // id -> полная карточка заявки (GET /api/requests/{id})
    resubmitComments: {},

    // ---- вкладка «Администрирование» (видна только user.is_admin) ----
    adminApprovers: [],
    adminBranches: [],
    newApprover: { id: "", name: "", email: "", role: "" },
    newBranch: { id: "", name: "" },

    // ---- подбор согласующего из корпоративного каталога (AD) ----
    directoryQuery: "",
    directoryResults: [],
    _directoryTimer: null,

    // ---- инициализация ----
    async init() {
      try {
        this.user = await api("/api/auth/me");
        await this.loadReferences();
      } catch {
        this.user = null;
      }
    },

    async loadMyRequests() {
      this.myRequestsLoading = true;
      try {
        const p = new URLSearchParams();
        if (this.myFilters.status) p.set("status", this.myFilters.status);
        if (this.myFilters.request_type) p.set("request_type", this.myFilters.request_type);
        if (this.myFilters.q) p.set("q", this.myFilters.q);
        const qs = p.toString();
        this.myRequests = await api("/api/requests" + (qs ? `?${qs}` : ""));
      } catch (e) {
        this.myRequests = [];
        this.notify("error", "Не удалось загрузить заявки", e.message);
      } finally {
        this.myRequestsLoading = false;
      }
    },

    toggleRequestDetails(r) {
      if (this.expandedId === r.id) {
        this.expandedId = null;
        return;
      }
      this.expandedId = r.id;
      api(`/api/requests/${r.id}`)
        .then((detail) => { this.requestDetails[r.id] = detail; })
        .catch(() => {});
    },

    requestTypeLabel(value) {
      const found = this.requestTypes.find((x) => x.value === value);
      return found ? found.label : (value || "");
    },

    canEditRequest(r) {
      if (!this.user || !r) return false;
      if (this.user.is_admin) return true;
      return r.status === "rework" || r.status === "rejected";
    },

    canResubmitRequest(r) {
      if (!this.user || !r) return false;
      if (this.user.is_admin) {
        return r.status === "rework" || r.status === "rejected" || r.status === "approved";
      }
      return r.status === "rework" || r.status === "rejected";
    },

    async resubmitRequest(r) {
      try {
        const comment = (this.resubmitComments[r.id] || "").trim();
        await api(`/api/requests/${r.id}/resubmit`, {
          method: "POST",
          body: JSON.stringify({ submission_comment: comment || null }),
        });
        this.resubmitComments[r.id] = "";
        delete this.requestDetails[r.id];
        await this.loadMyRequests();
        this.notify("success", "Заявка отправлена повторно", "Согласующие получат новое письмо.");
      } catch (e) {
        this.notify("error", "Не удалось отправить повторно", e.message);
      }
    },

    async editRequest(r) {
      try {
        const detail = this.requestDetails[r.id] || await api(`/api/requests/${r.id}`);
        this.requestDetails[r.id] = detail;
        const p = detail.payload || {};
        const blank = emptyForm();
        this.form = {
          ...blank,
          ...p,
          lab: { ...blank.lab, ...(p.lab || {}) },
          business: { ...blank.business, ...(p.business || {}) },
          prices: p.prices || [],
          approver_ids: p.approver_ids || [],
          mis_code: p.mis_code || "",
          submission_comment: detail.submission_comment || p.submission_comment || "",
        };
        this.editingId = r.id;
        this.submitted = false;
        this.showBusiness = true;
        this.activeTab = "new";
      } catch (e) {
        this.notify("error", "Не удалось открыть заявку", e.message);
      }
    },

    async deleteRequest(id) {
      if (!confirm("Удалить заявку? Это действие нельзя отменить.")) return;
      try {
        await api(`/api/requests/${id}`, { method: "DELETE" });
        this.myRequests = this.myRequests.filter((x) => x.id !== id);
        if (this.expandedId === id) this.expandedId = null;
        this.notify("success", "Заявка удалена", "Запись удалена из журнала.");
      } catch (e) {
        this.notify("error", "Не удалось удалить заявку", e.message);
      }
    },

    approverName(approverId) {
      const a = this.approvers.find((x) => x.id === approverId);
      return a ? a.name : approverId;
    },
    approvalChipClass(decision) {
      if (decision === "approve") return "border-green-200 bg-green-50 text-green-700";
      if (decision === "reject") return "border-red-200 bg-red-50 text-red-700";
      if (decision === "rework") return "border-amber-200 bg-amber-50 text-amber-700";
      return "border-slate-200 bg-slate-50 text-slate-500";
    },
    approvalChipMark(decision) {
      if (decision === "approve") return "✔";
      if (decision === "reject") return "✕";
      if (decision === "rework") return "↶";
      return "…";
    },
    historyDecisionLabel(decision) {
      return {
        approve: "Согласовано",
        reject: "Отклонено",
        rework: "На доработку",
        resubmit: "Повторная отправка",
      }[decision] || decision;
    },
    historyDecisionClass(decision) {
      return {
        approve: "bg-green-50 text-green-700",
        reject: "bg-red-50 text-red-700",
        rework: "bg-amber-50 text-amber-700",
        resubmit: "bg-blue-50 text-blue-700",
      }[decision] || "bg-slate-100 text-slate-600";
    },

    statusLabel(status) {
      return {
        pending: "На согласовании",
        approved: "Согласовано",
        rejected: "Отклонено",
        rework: "На доработке",
        imported: "Внесено в МИС",
      }[status] || status;
    },
    statusBadgeClass(status) {
      return {
        pending: "bg-amber-50 text-amber-700",
        approved: "bg-green-50 text-green-700",
        rejected: "bg-red-50 text-red-700",
        rework: "bg-amber-50 text-amber-700",
        imported: "bg-blue-50 text-blue-700",
      }[status] || "bg-slate-100 text-slate-600";
    },
    formatDate(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" }) +
        " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    },

    // ---- администрирование (согласующие/филиалы) ----
    async loadAdmin() {
      try {
        [this.adminApprovers, this.adminBranches] = await Promise.all([
          api("/api/admin/approvers"),
          api("/api/admin/branches"),
        ]);
      } catch (e) {
        this.notify("error", "Не удалось загрузить справочники", e.message);
      }
    },

    async addApprover() {
      if (!this.newApprover.id || !this.newApprover.name || !this.newApprover.email || !this.newApprover.role) {
        this.notify("error", "Не все поля заполнены", "Заполните id, ФИО, email и роль согласующего.");
        return;
      }
      try {
        await api("/api/admin/approvers", { method: "POST", body: JSON.stringify({ ...this.newApprover, required_for: ["all"] }) });
        this.newApprover = { id: "", name: "", email: "", role: "" };
        this.directoryQuery = "";
        this.directoryResults = [];
        await this.loadAdmin();
        await this.loadReferences(); // обновить список согласующих в форме «Новая заявка»
      } catch (e) {
        this.notify("error", "Не удалось добавить согласующего", e.message);
      }
    },

    async deleteApprover(id) {
      try {
        await api(`/api/admin/approvers/${id}`, { method: "DELETE" });
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.notify("error", "Не удалось удалить согласующего", e.message);
      }
    },

    async addBranch() {
      if (!this.newBranch.id || !this.newBranch.name) {
        this.notify("error", "Не все поля заполнены", "Заполните id и название филиала.");
        return;
      }
      try {
        await api("/api/admin/branches", { method: "POST", body: JSON.stringify(this.newBranch) });
        this.newBranch = { id: "", name: "" };
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.notify("error", "Не удалось добавить филиал", e.message);
      }
    },

    async deleteBranch(id) {
      try {
        await api(`/api/admin/branches/${id}`, { method: "DELETE" });
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.notify("error", "Не удалось удалить филиал", e.message);
      }
    },

    // ---- подбор согласующего из корпоративного каталога (AD) ----
    // В mock-режиме ищет по фиксированному демо-набору (см. ldap_auth._mock_search_directory);
    // в боевом — по реальному AD под сервисной учёткой, см. ldap_auth.search_directory.
    searchDirectory() {
      clearTimeout(this._directoryTimer);
      this._directoryTimer = setTimeout(() => this._doDirectorySearch(this.directoryQuery), 300);
    },
    async _doDirectorySearch(q) {
      if (!q || q.length < 2) { this.directoryResults = []; return; }
      try {
        this.directoryResults = await api(`/api/admin/directory/search?q=${encodeURIComponent(q)}&limit=10`);
      } catch (e) {
        this.directoryResults = [];
        this.notify("error", "Поиск по каталогу AD", e.message);
      }
    },
    pickDirectoryUser(u) {
      this.newApprover.name = u.name;
      this.newApprover.email = u.email;
      if (!this.newApprover.role) this.newApprover.role = u.position || "";
      if (!this.newApprover.id) this.newApprover.id = (u.login || u.email.split("@")[0] || "").toLowerCase();
      this.directoryResults = [];
      this.directoryQuery = "";
    },

    async loadReferences() {
      [this.branches, this.approvers] = await Promise.all([
        api("/api/dictionaries/branches"),
        api("/api/dictionaries/approvers"),
      ]);
    },

    async login() {
      this.loading = true;
      try {
        this.user = await api("/api/auth/login", { method: "POST", body: JSON.stringify(this.loginForm) });
        await this.loadReferences();
      } catch (e) {
        this.notify("error", "Не удалось войти", e.message);
      } finally {
        this.loading = false;
      }
    },

    async logout() {
      await api("/api/auth/logout", { method: "POST" });
      this.user = null;
      this.form = emptyForm();
      this.editingId = null;
    },

    // ---- цены по филиалам ----
    priceFor(branch) {
      const found = this.form.prices.find((p) => p.branch === branch);
      return found ? found.price : "";
    },
    setPriceFor(branch, value) {
      const price = parseFloat(value);
      this.form.prices = this.form.prices.filter((p) => p.branch !== branch);
      if (!isNaN(price) && price > 0) this.form.prices.push({ branch, price });
    },
    toggleAllBranches() {
      if (this.form.all_branches) this.applyUniformPrice();
    },
    applyUniformPrice() {
      if (!this.uniformPrice) return;
      this.form.prices = this.branches.map((b) => ({ branch: b, price: this.uniformPrice }));
    },

    // ---- автокомплит Минздрав (по названию услуги ИЛИ по отдельному полю поиска) ----
    debouncedMinzdravSearch() {
      clearTimeout(this._minzdravTimer);
      const q = this.minzdravQuery || this.form.name;
      this._minzdravTimer = setTimeout(() => this._doMinzdravSearch(q), 300);
    },
    searchMinzdrav() {
      this.debouncedMinzdravSearch();
    },
    async _doMinzdravSearch(q) {
      if (!q || q.length < 3) { this.minzdravResults = []; return; }
      try {
        this.minzdravResults = await api(`/api/dictionaries/minzdrav?q=${encodeURIComponent(q)}&limit=8`);
      } catch {
        this.minzdravResults = [];
      }
    },
    selectMinzdrav(r) {
      this.form.minzdrav = { code: r.code, name: r.name, manual_override: true };
      this.minzdravResults = [];
      this.minzdravQuery = "";
    },

    // ---- авто-подстановка кода Минздрава по мере ввода "Наименование услуги" ----
    // Отдельно от debouncedMinzdravSearch() выше: тот только наполняет выпадающий
    // список для РУЧНОГО выбора, а это — фоновая подсказка, которая сама
    // проставляет form.minzdrav, если бэкенд достаточно уверен в совпадении
    // (см. GET /api/dictionaries/minzdrav/suggest, порог — DictionaryStore.
    // best_minzdrav_match_scored). Не перезаписывает выбор, который пользователь
    // сделал вручную (manual_override=true) или явно отклонил крестиком.
    _minzdravDismissed: false,
    debouncedMinzdravAutoSuggest() {
      clearTimeout(this._minzdravAutoTimer);
      this._minzdravDismissed = false; // новый текст — пользователь ещё не успел отклонить именно его
      this._minzdravAutoTimer = setTimeout(() => this._doMinzdravAutoSuggest(this.form.name), 400);
    },
    async _doMinzdravAutoSuggest(name) {
      if (!name || name.trim().length < 4) return;
      if (this.form.minzdrav && this.form.minzdrav.manual_override) return;
      if (this._minzdravDismissed) return;
      try {
        const match = await api(`/api/dictionaries/minzdrav/suggest?name=${encodeURIComponent(name)}`);
        if (match && !(this.form.minzdrav && this.form.minzdrav.manual_override) && !this._minzdravDismissed) {
          this.form.minzdrav = { code: match.code, name: match.name, manual_override: false, match_score: match.score };
        }
      } catch {
        // фоновая подсказка необязательна для отправки заявки — молча пропускаем сбой,
        // не мешаем пользователю всплывающим окном из-за необязательной функции
      }
    },
    dismissMinzdravSuggestion() {
      this._minzdravDismissed = true;
      this.form.minzdrav = null;
    },

    // ---- автокомплит ЕРЛУ-КДЛ ----
    searchErlu() {
      clearTimeout(this._erluTimer);
      this._erluTimer = setTimeout(() => this._doErluSearch(this.erluQuery), 300);
    },
    async _doErluSearch(q) {
      if (!q || q.length < 3) { this.erluResults = []; return; }
      try {
        this.erluResults = await api(`/api/dictionaries/erlu?q=${encodeURIComponent(q)}&limit=10`);
      } catch {
        this.erluResults = [];
      }
    },
    selectErlu(r) {
      this.form.lab.erlu_code = r.erlu_code;
      this.form.lab.erlu_name = r.erlu_name;
      this.form.lab.lab_code = r.lab_code;
      this.form.lab.lab_name = r.lab_name;
      this.form.lab.is_composite = r.is_composite;
      this.erluQuery = r.erlu_name;
      this.erluResults = [];
    },

    // ---- отправка ----
    async submitRequest() {
      if (!this.form.prices.length) {
        this.notify("error", "Не хватает данных", "Укажите хотя бы одну цену по филиалу.");
        return;
      }
      if (!this.form.approver_ids.length) {
        this.notify("error", "Не хватает данных", "Выберите хотя бы одного согласующего.");
        return;
      }
      if (!(this.form.business && (this.form.business.description || "").trim())) {
        this.notify("error", "Не хватает данных", "Укажите деловое обоснование (суть услуги).");
        this.showBusiness = true;
        return;
      }
      this.loading = true;
      try {
        const payload = JSON.parse(JSON.stringify(this.form));
        if (!payload.is_lab_service) payload.lab = null;
        if (!payload.date_start) payload.date_start = null;
        if (!payload.date_end) payload.date_end = null;
        if (!payload.mis_code) payload.mis_code = null;
        if (this.editingId) {
          await api(`/api/requests/${this.editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
          this.notify("success", "Изменения сохранены", "Карточка заявки обновлена.");
          this.editingId = null;
          this.form = emptyForm();
          this.activeTab = "my";
          await this.loadMyRequests();
        } else {
          const res = await api("/api/requests", { method: "POST", body: JSON.stringify(payload) });
          this.submitted = true;
          this.submittedId = res.id;
        }
      } catch (e) {
        this.notify("error", this.editingId ? "Не удалось сохранить заявку" : "Не удалось отправить заявку", e.message);
      } finally {
        this.loading = false;
      }
    },

    resetForm() {
      this.form = emptyForm();
      this.editingId = null;
      this.uniformPrice = null;
      this.submitted = false;
      this.submittedId = null;
    },
  };
}
