# vendor/ — офлайн-копии зависимостей фронтенда

`alpine.min.js` и `tailwind.css` лежат здесь как файлы, а не подключаются с
`cdn.tailwindcss.com` / `cdn.jsdelivr.net`, потому что без выхода в интернет
(типичная ситуация для внутренней сети клиники, где крутится AD/SMTP/МИС)
страница с CDN-скриптами открывается ПОЛНОСТЬЮ ПУСТОЙ — Alpine.js без
загрузки не инициализирует ни один `x-data`/`x-show`/`x-if` на странице.
Обнаружено и воспроизведено при smoke-тестировании (раздел 12.7 ТЗ.md).

## Пересборка после изменений в index.html / app.js

`tailwind.css` собран статически под конкретный набор классов, которые
реально встречаются в `frontend/index.html` и `frontend/app.js` на момент
сборки. Если вы добавите новый Tailwind-класс (например, `bg-purple-50`) в
разметку, его не будет в `tailwind.css`, пока вы не пересоберёте:

```bash
npm install --no-save tailwindcss@3.4.13
npx tailwindcss \
  -i <(cat <<'EOF'
@tailwind base;
@tailwind components;
@tailwind utilities;
@layer components {
  .field-label { @apply block text-sm font-medium text-slate-700 mb-1; }
  .input { @apply w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500; }
  .card { @apply bg-white rounded-2xl shadow-sm border border-slate-200 p-6; }
  .btn-primary { @apply inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white font-medium px-5 py-2.5 rounded-lg text-sm transition; }
  .btn-secondary { @apply inline-flex items-center gap-2 bg-white border border-slate-300 hover:bg-slate-50 text-slate-700 font-medium px-5 py-2.5 rounded-lg text-sm transition; }
  .chip { @apply inline-flex items-center gap-1 bg-blue-50 text-blue-700 border border-blue-200 rounded-full px-3 py-1 text-xs font-medium; }
}
EOF
) \
  -o frontend/vendor/tailwind.css --minify \
  --content "frontend/index.html,frontend/app.js"
```

(Проще: сохраните блок `@tailwind ...` выше в `input.css` и запустите
`npx tailwindcss -i input.css -o frontend/vendor/tailwind.css --minify` с
`tailwind.config.js`, где `content` указывает на `index.html` и `app.js`.)

## Пересборка Alpine.js

Не нужна, пока не хотите сменить версию — это готовый minified-бандл:

```bash
npm install --no-save alpinejs@3.14.1
cp node_modules/alpinejs/dist/cdn.min.js frontend/vendor/alpine.min.js
```

## Требует ли это доступа в интернет на сервере в проде?

Нет. `npm install`/`npx tailwindcss` нужны только один раз, на машине
разработчика при сборке — сами `alpine.min.js` и `tailwind.css` после этого
статические файлы, отдаются вашим же FastAPI (`StaticFiles`, см.
`backend/app/main.py`) вместе с остальным фронтендом. На проде/у пользователей
в браузере никакого обращения к внешним CDN не происходит.
