import sys

from loguru import logger

# Remove default handler and add custom format
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>llm_gateway</cyan> | <level>{message}</level>",
    level="INFO",
    colorize=True,
)
