# config.py
"""
Configuration manager for the NQ Opening Forecast System.
Loads and validates settings from environment variables or a local .env file.
"""

import os
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

class Config:
    # Database Settings
    #   DB_PATH     - the long-lived production store (collector + pipeline write here)
    #   DEV_DB_PATH - throwaway store for populate_mock_data.py / experiments
    DB_PATH = os.getenv("DB_PATH", "data/trading_pipeline.db")
    DEV_DB_PATH = os.getenv("DEV_DB_PATH", "data/trading_pipeline.dev.db")
    SCHEMA_PATH = os.getenv("SCHEMA_PATH", os.path.join(_PROJECT_ROOT, "database", "schema.sql"))

    # IBKR Connection Settings
    IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
    # 4002 = Gateway paper, 4001 = Gateway live, 7497 = TWS paper, 7496 = TWS live
    IB_PORT = int(os.getenv("IB_PORT", "4002"))
    IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

    # LLM Settings
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")  # e.g. gpt-4o-mini, claude-3-5-sonnet-latest

    # Application Settings
    TIMEZONE = "America/New_York"
    FEATURE_VERSION = "v1.0"
    PROMPT_VERSION = "v1.0"

    @classmethod
    def validate(cls):
        """Validates critical config values."""
        # Ensure data folder directory exists
        db_dir = os.path.dirname(cls.DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        # Check LLM keys (optional warnings rather than hard crashes)
        if not cls.OPENAI_API_KEY and not cls.ANTHROPIC_API_KEY:
            print("WARNING: Neither OPENAI_API_KEY nor ANTHROPIC_API_KEY found in environment variables.")

if __name__ == "__main__":
    Config.validate()
    print("Database Path:", Config.DB_PATH)
    print("IBKR Gateway Port:", Config.IB_PORT)
