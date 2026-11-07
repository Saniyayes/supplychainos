"""
SupplyChainOS — FastAPI application entry point.
"""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from supplychainos.api.routes import router
from supplychainos.config import settings
from supplychainos.logging_config import configure_logging, get_logger
from supplychainos.persistence.database import init_db

configure_logging()
logger = get_logger(__name__)

app = FastAPI(
    title="SupplyChainOS",
    description=(
        "Autonomous Supply Chain Planning, Execution and Recovery Engine.\n\n"
        "A portfolio project demonstrating agentic AI workflows, OR-Tools optimisation, "
        "multi-type memory, and robust state-machine execution."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
def on_startup():
    logger.info("supplychainos.startup", env=settings.app_env)
    init_db()
    logger.info("database.initialised")


@app.get("/", tags=["System"])
def root():
    return {
        "name": "SupplyChainOS",
        "version": "1.0.0",
        "docs": "/docs",
        "api_prefix": "/api/v1",
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env == "development",
    )
