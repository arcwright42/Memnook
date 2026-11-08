import argparse
import asyncio
import json
import logging
import signal
from importlib.resources import files
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from memory_mcp.application.extraction import Extractor
from memory_mcp.application.service import MemoryService
from memory_mcp.application.worker import Worker
from memory_mcp.config import Settings
from memory_mcp.domain.errors import MemoryError
from memory_mcp.interfaces.mcp import create_app
from memory_mcp.providers.http import HTTPModels
from memory_mcp.storage.db import Database
from memory_mcp.storage.repository import Repository
from memory_mcp.storage.tasks import TaskQueue


def migration_config() -> Config:
    bundled = Path(str(files("memory_mcp").joinpath("migrations")))
    location = bundled if bundled.is_dir() else Path(__file__).resolve().parents[3] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(location))
    return config


def main():
    parser = argparse.ArgumentParser(prog="memory", description="Shared MCP memory service")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the Streamable HTTP MCP server")
    worker_parser = commands.add_parser("worker", help="Process durable extraction tasks")
    worker_parser.add_argument("--once", action="store_true", help="Process at most one ready task")
    commands.add_parser("migrate", help="Apply database migrations")
    retry_parser = commands.add_parser("retry", help="Requeue a failed raw record")
    retry_parser.add_argument("--tag", required=True)
    retry_parser.add_argument("--raw-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if args.command == "migrate":
        command.upgrade(migration_config(), "head")
        return
    db = Database(settings.database_url)
    models = HTTPModels(settings)
    repository = Repository(db, settings)

    async def close():
        await models.close()
        await db.close()

    async def background():
        try:
            if args.command == "retry":
                await TaskQueue(db, settings).retry(args.tag, args.raw_id)
                print(json.dumps({"raw_id": args.raw_id, "processing_status": "pending"}))
                return
            if not settings.llm_model or not settings.embedding_model:
                raise MemoryError(
                    "MODEL_NOT_CONFIGURED",
                    "Configure MEMORY_LLM_MODEL and MEMORY_EMBEDDING_MODEL before starting the worker.",
                )
            worker = Worker(repository, Extractor(repository, models, models), settings)
            if args.once:
                await worker.run_once()
                return
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await worker.run(stop)
        finally:
            await close()

    try:
        if args.command == "serve":
            service = MemoryService(repository, models, models, settings)
            uvicorn.run(
                create_app(service, settings, close), host=settings.host, port=settings.port
            )
        else:
            asyncio.run(background())
    except MemoryError as error:
        parser.exit(1, json.dumps(error.as_dict()) + "\n")


if __name__ == "__main__":
    main()
