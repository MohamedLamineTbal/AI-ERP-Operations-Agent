"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_ADMIN_URL = os.getenv("DATABASE_ADMIN_URL")
READONLY_DATABASE_URL = os.getenv("READONLY_DATABASE_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
ACCESS_KEY = os.getenv("ERP_ACCESS_KEY")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env and add the URL "
        "of the login the app runs as; seed.py creates that login."
    )
if not READONLY_DATABASE_URL:
    raise RuntimeError(
        "READONLY_DATABASE_URL is not set. Add the URL of the login that "
        "model-generated SQL runs as to .env; seed.py creates that login."
    )
