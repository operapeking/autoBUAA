from __future__ import annotations

import httpx
from app.buaa.sso import BuaaSessionManager
from app.core.config import Settings, get_settings
from app.core.crypto import SecretCipher
from app.core.logging import configure_logging
from app.core.time import Clock
from app.db.base import build_engine, build_session_factory
from app.db.init_db import init_database
from app.framework.scheduler import UnifiedScheduler
from app.framework.tasks import TaskRegistry
from app.modules.automation import reconcile_automation
from app.modules.tasks import register_all_tasks
from app.notifications.service import NotificationDispatcher


class AppContainer:
    """Wires application settings, DB, BUAA adapters, notifications, and unified scheduler."""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        buaa_transport: httpx.AsyncBaseTransport | None = None,
        bark_transport: httpx.AsyncBaseTransport | None = None,
        run_migrations: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        configure_logging(
            self.settings.log_level, self.settings.data_dir / "logs" / "autobuaa.log"
        )

        self.clock = clock or Clock()
        self.cipher = SecretCipher(self.settings.resolved_secret_key_file)
        self.engine = build_engine(self.settings.db_url)
        self.session_factory = build_session_factory(self.engine)

        if run_migrations:
            init_database(self.engine, self.settings.db_url, self.session_factory)
        else:
            from app.db.base import Base
            from app.db.init_db import seed_defaults

            Base.metadata.create_all(self.engine)
            with self.session_factory() as session:
                seed_defaults(session)

        self.registry = TaskRegistry()
        self.session_mgr = BuaaSessionManager(
            self.session_factory,
            self.cipher,
            self.settings.captcha_dir,
            clock=self.clock,
            transport=buaa_transport,
        )
        register_all_tasks(self.registry, self.session_mgr)

        with self.session_factory() as session:
            self.registry.sync_to_db(session)
            reconcile_automation(session, self.registry, self.clock)
            session.commit()

        self.notification_dispatcher = NotificationDispatcher(
            self.session_factory,
            self.cipher,
            clock=self.clock,
            transport=bark_transport,
        )
        self.scheduler = UnifiedScheduler(
            self.session_factory,
            self.registry,
            self.cipher,
            self.notification_dispatcher,
            clock=self.clock,
            concurrency=self.settings.scheduler_concurrency,
            poll_seconds=self.settings.scheduler_poll_seconds,
        )
