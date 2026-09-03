# Import models so SQLAlchemy registers them before create_all runs.
from app import models  # noqa: F401
from app.db.base import Base
from app.db.session import engine


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
