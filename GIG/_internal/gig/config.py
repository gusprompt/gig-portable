import os
from pathlib import Path
from dotenv import load_dotenv

# Carrega .env do diretorio do projeto
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Modelos disponiveis
MODELS = {
    "Gemini 2.5 Pro": "gemini-2.5-pro",
    "Gemini 2.0 Flash": "gemini-2.0-flash",
    "Gemini 3 Pro Preview": "gemini-3-pro-preview",
    "Gemini 3 Flash Preview": "gemini-3-flash-preview",
}

# Retries
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # segundos (backoff exponencial)

# Pasta de saida (relativa a pasta de entrada)
OUTPUT_DIR_NAME = "gig_output"
