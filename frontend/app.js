// Логика формы. Без сборщика — чистый скрипт под Alpine.js, чтобы Cursor/разработчик
// мог сразу править и запускать без npm install / webpack.

const API_BASE = ""; // фронтенд раздаётся тем же FastAPI-приложением (см. main.py, StaticFiles на "/")
// Если фронтенд разворачивается отдельно (Nginx/CDN) — укажите здесь адрес бэкенда,
// например: const API_BASE = "https://price-form.clinic.local";

async function api(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка запроса: ${res.status}`);
  }
  // 204 No Content (например, DELETE) — тела нет, res.json() упал бы на пустой строке.
  if (res.status === 204) return null;
  return res.json();
}

function emptyForm() {
  return {
    request_type: "new_service",
    name: "",
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
  };
}

function app() {
  return {
    // ---- состояние ----
    user: null,
    loading: false,
    loginForm: { login: "", password: "" },
    loginError: "",

    form: emptyForm(),
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
    showBusiness: false,

    minzdravQuery: "",
    minzdravResults: [],
    _minzdravTimer: null,

    erluQuery: "",
    erluResults: [],
    _erluTimer: null,

    submitted: false,
    submittedId: null,
    submitError: "",

    // ---- вкладка «Мои заявки» ----
    activeTab: "new", // 'new' | 'my' | 'admin'
    myRequests: [],
    myRequestsLoading: false,
    expandedId: null,
    requestDetails: {}, // id -> полная карточка заявки (GET /api/requests/{id}), кэш на сессию

    // ---- вкладка «Администрирование» (видна только user.is_admin) ----
    adminApprovers: [],
    adminBranches: [],
    newApprover: { id: "", name: "", email: "", role: "" },
    newBranch: { id: "", name: "" },
    adminError: "",

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
        this.myRequests = await api("/api/requests");
      } catch (e) {
        this.myRequests = [];
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
      if (!this.requestDetails[r.id]) {
        api(`/api/requests/${r.id}`)
          .then((detail) => { this.requestDetails[r.id] = detail; })
          .catch(() => {});
      }
    },

    approverName(approverId) {
      const a = this.approvers.find((x) => x.id === approverId);
      return a ? a.name : approverId;
    },

    statusLabel(status) {
      return { pending: "На согласовании", approved: "Согласовано", rejected: "Отклонено", imported: "Внесено в МИС" }[status] || status;
    },
    statusBadgeClass(status) {
      return {
        pending: "bg-amber-50 text-amber-700",
        approved: "bg-green-50 text-green-700",
        rejected: "bg-red-50 text-red-700",
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
      this.adminError = "";
      try {
        [this.adminApprovers, this.adminBranches] = await Promise.all([
          api("/api/admin/approvers"),
          api("/api/admin/branches"),
        ]);
      } catch (e) {
        this.adminError = e.message;
      }
    },

    async addApprover() {
      this.adminError = "";
      if (!this.newApprover.id || !this.newApprover.name || !this.newApprover.email || !this.newApprover.role) {
        this.adminError = "Заполните все поля согласующего.";
        return;
      }
      try {
        await api("/api/admin/approvers", { method: "POST", body: JSON.stringify({ ...this.newApprover, required_for: ["all"] }) });
        this.newApprover = { id: "", name: "", email: "", role: "" };
        await this.loadAdmin();
        await this.loadReferences(); // обновить список согласующих в форме «Новая заявка»
      } catch (e) {
        this.adminError = e.message;
      }
    },

    async deleteApprover(id) {
      this.adminError = "";
      try {
        await api(`/api/admin/approvers/${id}`, { method: "DELETE" });
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.adminError = e.message;
      }
    },

    async addBranch() {
      this.adminError = "";
      if (!this.newBranch.id || !this.newBranch.name) {
        this.adminError = "Заполните id и название филиала.";
        return;
      }
      try {
        await api("/api/admin/branches", { method: "POST", body: JSON.stringify(this.newBranch) });
        this.newBranch = { id: "", name: "" };
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.adminError = e.message;
      }
    },

    async deleteBranch(id) {
      this.adminError = "";
      try {
        await api(`/api/admin/branches/${id}`, { method: "DELETE" });
        await this.loadAdmin();
        await this.loadReferences();
      } catch (e) {
        this.adminError = e.message;
      }
    },

    async loadReferences() {
      [this.branches, this.approvers] = await Promise.all([
        api("/api/dictionaries/branches"),
        api("/api/dictionaries/approvers"),
      ]);
    },

    async login() {
      this.loading = true;
      this.loginError = "";
      try {
        this.user = await api("/api/auth/login", { method: "POST", body: JSON.stringify(this.loginForm) });
        await this.loadReferences();
      } catch (e) {
        this.loginError = e.message;
      } finally {
        this.loading = false;
      }
    },

    async logout() {
      await api("/api/auth/logout", { method: "POST" });
      this.user = null;
      this.form = emptyForm();
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
      this.submitError = "";
      if (!this.form.prices.length) {
        this.submitError = "Укажите хотя бы одну цену по филиалу.";
        return;
      }
      if (!this.form.approver_ids.length) {
        this.submitError = "Выберите хотя бы одного согласующего.";
        return;
      }
      this.loading = true;
      try {
        const payload = JSON.parse(JSON.stringify(this.form));
        if (!payload.is_lab_service) payload.lab = null;
        const res = await api("/api/requests", { method: "POST", body: JSON.stringify(payload) });
        this.submitted = true;
        this.submittedId = res.id;
      } catch (e) {
        this.submitError = e.message;
      } finally {
        this.loading = false;
      }
    },

    resetForm() {
      this.form = emptyForm();
      this.uniformPrice = null;
      this.submitted = false;
      this.submittedId = null;
    },
  };
}
