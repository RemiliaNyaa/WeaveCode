from weavecode.core.storage import schema  # noqa: F401  导入即注册迁移
from weavecode.core.storage.database import DEFAULT_DB_PATH, Database
from weavecode.core.storage.migrations import MIGRATIONS, apply_migrations

__all__ = ["DEFAULT_DB_PATH", "MIGRATIONS", "Database", "apply_migrations"]
