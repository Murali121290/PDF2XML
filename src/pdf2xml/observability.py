"""Small JSON log formatter suitable for collectors and alert rules."""
import json
import logging
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for key in ("job_id", "stage", "state", "elapsed_s", "queue_wait_s", "status_code", "request_id"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging() -> None:
    logger = logging.getLogger("pdf2xml")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
