import os
import logging
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
connect_args = {}

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./app.db"
    connect_args["check_same_thread"] = False
    logger.warning("DATABASE_URL is not set. Falling back to local SQLite database at ./app.db")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)
