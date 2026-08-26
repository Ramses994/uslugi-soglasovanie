from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import get_settings
from .database import Base, engine
from .services import admin_store
from .services.dictionaries import init_dictionaries
from .routers import admin, auth, dictionaries, requests as requests_router, approvals

Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_dictionaries()
    settings = get_settings()
    admin_store.ensure_seed(settings.branches_json, settings.branches_seed)
    yield


app = FastAPI(
    title="Форма согласования услуг МИС Медиалог",
    version="1.0.0",
    description="Веб-сервис подачи и согласования заявок на добавление услуг/продуктов в прейскурант МИС «Медиалог»",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # в проде — сузить до домена фронтенда
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(dictionaries.router)
app.include_router(requests_router.router)
app.include_router(approvals.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Отдаём фронтенд как статику (для простого single-VM деплоя без отдельного nginx).
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
